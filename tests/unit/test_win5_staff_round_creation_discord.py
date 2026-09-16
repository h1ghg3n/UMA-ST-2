"""Discord adapter tests for WIN5 staff Round creation workflows."""

from __future__ import annotations

from win5_staff_discord_test_support import (
    UTC,
    DatabaseRuntime,
    RecordingAuthorization,
    RecordingCreationCommands,
    RecordingCreationQueries,
    RecordingInteraction,
    RecordingPreparation,
    RecordingQueries,
    Win5NormalRoundCreationModal,
    Win5NormalRoundCreationPreviewView,
    Win5NormalRoundEntryCorrectionModal,
    Win5RoundCreationSeasonChoice,
    Win5RoundCreationSeasonPage,
    Win5RoundCreationSeasonView,
    Win5RoundCreationUnavailableError,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SpecialRoundCreationModal,
    Win5StaffCommandGroup,
    Win5StaffInteractionContext,
    _adapter,
    _created_round,
    _creation_season,
    _layout_button,
    _layout_items,
    _layout_text,
    _normal_creation_draft,
    _normal_creation_modal,
    app_commands,
    asyncio,
    build_normal_round_creation_draft,
    compose_win5_command_group,
    create_engine,
    datetime,
    discord,
    format_normal_round_creation_preview,
    format_round_creation_success,
    get_ident,
    parse_normal_round_entries,
    parse_optional_normal_race_schedule,
    parse_special_round_race_names,
    pytest,
)


def _track_stop(view: discord.ui.View | discord.ui.LayoutView) -> list[bool]:
    original_stop = view.stop
    calls: list[bool] = []

    def record_stop() -> None:
        calls.append(True)
        original_stop()

    view.stop = record_stop  # type: ignore[method-assign]
    return calls


def _track_stop_before_edit(
    view: discord.ui.View | discord.ui.LayoutView,
    interaction: RecordingInteraction,
    *,
    deferred: bool = False,
    fail: bool = False,
) -> tuple[list[bool], list[bool]]:
    stop_calls = _track_stop(view)
    stopped_at_edit: list[bool] = []
    target = interaction if deferred else interaction.response
    attribute = "edit_original_response" if deferred else "edit_message"
    original_edit = getattr(target, attribute)

    async def observe_edit(**kwargs: object) -> None:
        stopped_at_edit.append(bool(stop_calls))
        if fail:
            raise RuntimeError("Discord component edit failed")
        await original_edit(**kwargs)

    setattr(target, attribute, observe_edit)
    return stop_calls, stopped_at_edit


def test_staff_round_registration_and_action_snapshot_open_private_panel() -> None:
    authorization = RecordingAuthorization()
    adapter = _adapter(RecordingQueries(), authorization=authorization)
    group = Win5StaffCommandGroup(adapter=adapter)
    command = group.get_command("round")
    interaction = RecordingInteraction()

    assert group.name == "staff"
    assert group.description == "WIN5 스태프 전용 기능입니다."
    assert [registered.name for registered in group.commands] == ["round", "season"]
    assert isinstance(command, app_commands.Command)
    assert command.description == "WIN5 라운드 생성·삭제·상태·결과·채점·특별 취소를 관리합니다."
    assert command.parameters == []

    asyncio.run(command.callback(group, interaction))  # type: ignore[arg-type,union-attr]

    assert authorization.calls == [(interaction, "win5.staff.round")]
    content, options = interaction.response.messages[0]
    assert content == "관리할 WIN5 라운드 작업을 선택해 주세요."
    assert options["ephemeral"] is True
    view = options["view"]
    assert isinstance(view, discord.ui.View)
    select = view.children[0]
    assert isinstance(select, discord.ui.Select)
    assert [(option.value, option.label) for option in select.options] == [
        ("create", "일반 라운드 생성"),
        ("special-create", "특별 라운드 생성"),
        ("round-delete", "setup 라운드 삭제"),
        ("round-open", "라운드 열기"),
        ("round-close", "라운드 마감"),
        ("entry", "일반 결과 입력"),
        ("correction", "일반 결과 정정"),
        ("special-result-entry", "특별 결과 입력"),
        ("special-result-correction", "특별 결과 정정"),
        ("special-void", "특별 Race 취소·복원"),
        ("special-round-cancel", "특별 라운드 전체 취소"),
        ("normal-scoring", "일반 라운드 채점"),
        ("special-scoring", "특별 라운드 채점"),
    ]


def test_round_creation_action_uses_worker_query_and_operator_title_season_selector() -> None:
    page = Win5RoundCreationSeasonPage(
        offset=0,
        limit=25,
        choices=(
            Win5RoundCreationSeasonChoice(
                id=7,
                name="2026 @everyone 하반기",
                status=Win5SeasonStatus.ACTIVE,
            ),
            Win5RoundCreationSeasonChoice(
                id=8,
                name="2026 @everyone 하반기",
                status=Win5SeasonStatus.ACTIVE,
            ),
        ),
    )
    creation_queries = RecordingCreationQueries(page=page)
    authorization = RecordingAuthorization()
    adapter = _adapter(
        RecordingQueries(),
        creation_queries=creation_queries,
        authorization=authorization,
    )
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    from uma_st2.adapters.discord.win5_staff import Win5StaffRoundActionView

    source_view = Win5StaffRoundActionView(adapter=adapter, context=context)
    select = source_view.children[0]
    select._values = ["create"]
    event_loop_thread_id = get_ident()
    stop_calls, source_stopped_at_edit = _track_stop_before_edit(source_view, interaction)

    asyncio.run(select.callback(interaction))  # type: ignore[arg-type]

    assert source_stopped_at_edit == [True]
    assert stop_calls == [True]
    assert authorization.calls == [(interaction, "win5.staff.round")]
    assert creation_queries.calls[0][:2] == (0, 25)
    assert creation_queries.calls[0][2] != event_loop_thread_id
    edit = interaction.response.edits[0]
    assert edit["content"].startswith("Normal Round를 생성할 Season을 선택해 주세요. (페이지 1)")
    assert "⚠️" in edit["content"]
    assert "참고 기간은 자동 lifecycle 기준이 아닙니다" in edit["content"]
    view = edit["view"]
    assert isinstance(view, Win5RoundCreationSeasonView)
    assert [option.value for option in view.children[0].options] == ["7", "8"]
    assert [option.label for option in view.children[0].options] == [
        "2026 @\u200beveryone 하반기 · ID 7",
        "2026 @\u200beveryone 하반기 · ID 8",
    ]
    assert [option.description for option in view.children[0].options] == [
        "⚠ 진행 중 · 참고 기간 미완성",
        "⚠ 진행 중 · 참고 기간 미완성",
    ]


def test_round_creation_season_pagination_stops_existing_view_before_replacement() -> None:
    next_page = Win5RoundCreationSeasonPage(
        offset=25,
        limit=25,
        choices=(_creation_season(),),
        has_previous=True,
    )
    creation_queries = RecordingCreationQueries(page=next_page)
    adapter = _adapter(RecordingQueries(), creation_queries=creation_queries)
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5RoundCreationSeasonView(
        adapter=adapter,
        context=context,
        round_type=Win5RoundType.NORMAL,
        page=Win5RoundCreationSeasonPage(
            offset=0,
            limit=25,
            choices=(_creation_season(),),
            has_next=True,
        ),
    )
    next_button = next(child for child in source_view.children if isinstance(child, discord.ui.Button))
    stop_calls, source_stopped_at_edit = _track_stop_before_edit(source_view, interaction)

    asyncio.run(next_button.callback(interaction))  # type: ignore[arg-type]

    assert creation_queries.calls[0][:2] == (25, 25)
    assert source_stopped_at_edit == [True]
    assert stop_calls == [True]
    replacement = interaction.response.edits[0]["view"]
    assert isinstance(replacement, Win5RoundCreationSeasonView)


def test_round_creation_without_eligible_season_stops_with_emphasized_zero_write_warning() -> None:
    page = Win5RoundCreationSeasonPage(offset=0, limit=25)
    creation_queries = RecordingCreationQueries(page=page)
    creation_commands = RecordingCreationCommands(result=_created_round())
    adapter = _adapter(
        RecordingQueries(),
        creation_queries=creation_queries,
        creation_commands=creation_commands,
    )
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)

    asyncio.run(
        adapter.show_round_creation_seasons(  # type: ignore[arg-type]
            interaction,
            context=context,
            round_type=Win5RoundType.NORMAL,
            offset=0,
        )
    )

    assert creation_queries.calls[0][:2] == (0, 25)
    assert creation_commands.calls == []
    assert interaction.response.modals == []
    content, options = interaction.response.messages[0]
    assert "## ⚠️ WIN5 Round 생성 불가" in content
    assert "draft 또는 active Season이 없습니다" in content
    assert options["ephemeral"] is True


def test_creation_season_warnings_cover_draft_and_incomplete_reference_period() -> None:
    starts_at = datetime(2026, 8, 1, tzinfo=UTC)
    season = _creation_season(
        status=Win5SeasonStatus.DRAFT,
        starts_at=starts_at,
    )
    draft = build_normal_round_creation_draft(
        season=season,
        round_name="Round",
        race_name="Race",
        race_schedule="",
        gate_numbers="1\n2\n3\n4\n5",
        horse_names="가\n나\n다\n라\n마",
    )

    preview = format_normal_round_creation_preview(draft)

    assert "Season 참고 기간: 2026-08-01 09:00 KST ~ 미지정" in preview
    assert "Season 경고: 선택한 Season은 아직 준비 중(draft)입니다" in preview
    assert "Season 경고: Season 참고 종료 일정이 미지정입니다" in preview


def test_complete_active_season_opens_creation_modal_without_warning_marker() -> None:
    season = _creation_season(
        starts_at=datetime(2026, 8, 1, tzinfo=UTC),
        ends_at=datetime(2026, 12, 31, tzinfo=UTC),
    )
    adapter = _adapter(RecordingQueries())
    interaction = RecordingInteraction()

    normal = Win5NormalRoundCreationModal(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(interaction),
        season=season,
    )
    special = Win5SpecialRoundCreationModal(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(interaction),
        season=season,
    )

    assert normal.title == "WIN5 일반 라운드 생성"
    assert special.title == "WIN5 특별 라운드 생성"


@pytest.mark.parametrize(
    ("round_type", "modal_type", "field_count"),
    [
        (Win5RoundType.NORMAL, Win5NormalRoundCreationModal, 5),
        (Win5RoundType.SPECIAL, Win5SpecialRoundCreationModal, 3),
    ],
)
def test_creation_season_selection_opens_text_input_only_modal_without_defer(
    round_type: Win5RoundType,
    modal_type: type[discord.ui.Modal],
    field_count: int,
) -> None:
    authorization = RecordingAuthorization()
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        authorization=authorization,
        preparation=preparation,
    )
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    page = Win5RoundCreationSeasonPage(
        offset=0,
        limit=25,
        choices=(
            Win5RoundCreationSeasonChoice(
                id=7,
                name="2026 하반기",
                status=Win5SeasonStatus.ACTIVE,
            ),
        ),
    )
    view = Win5RoundCreationSeasonView(
        adapter=adapter,
        context=context,
        round_type=round_type,
        page=page,
    )
    select = view.children[0]
    select._values = ["7"]

    asyncio.run(select.callback(interaction))  # type: ignore[arg-type]

    assert authorization.calls == [(interaction, "win5.staff.round")]
    assert preparation.calls == []
    assert len(interaction.response.modals) == 1
    modal = interaction.response.modals[0]
    assert isinstance(modal, modal_type)
    assert modal.title.startswith("⚠ ")
    assert len(modal.children) == field_count
    assert all(isinstance(child, discord.ui.TextInput) for child in modal.children)


def test_normal_creation_modal_builds_complete_bulk_preview_without_command_or_uow() -> None:
    creation_commands = RecordingCreationCommands(result=_created_round())
    creation_queries = RecordingCreationQueries()
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        creation_queries=creation_queries,
        creation_commands=creation_commands,
        preparation=preparation,
    )
    interaction = RecordingInteraction(interaction_id=991)
    modal = _normal_creation_modal(adapter, interaction)

    asyncio.run(modal.on_submit(interaction))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "win5.staff.round", True)]
    assert creation_queries.calls == []
    assert creation_commands.calls == []
    assert modal.gate_numbers.style == discord.TextStyle.paragraph
    assert modal.horse_names.style == discord.TextStyle.paragraph
    edit = interaction.edits[0]
    assert edit["content"] is None
    view = edit["view"]
    assert isinstance(view, Win5NormalRoundCreationPreviewView)
    assert tuple((entry.gate_number, entry.horse_name) for entry in view.draft.entries) == (
        (1, "스페셜 위크"),
        (2, "사일런스 스즈카"),
        (4, "토카이 테이오"),
        (7, "메지로 맥퀸"),
        (8, "라이스 샤워"),
    )
    text = _layout_text(view)
    assert "2026 @\u200beveryone 하반기" in text
    assert "제3회 아리마 기념" in text
    assert "아리마 기념" in text
    assert "2026-08-30 15:30 KST" in text
    assert "1 · 스페셜 위크" in text
    assert "8 · 라이스 샤워" in text
    correction_selects = _layout_items(view, discord.ui.Select)
    assert len(correction_selects) == 1
    assert [option.label for option in correction_selects[0].options] == [
        "1 · 스페셜 위크",
        "2 · 사일런스 스즈카",
        "4 · 토카이 테이오",
        "7 · 메지로 맥퀸",
        "8 · 라이스 샤워",
    ]


def test_normal_creation_modal_rejects_mismatched_entry_lists_before_command() -> None:
    creation_commands = RecordingCreationCommands(result=_created_round())
    adapter = _adapter(
        RecordingQueries(),
        creation_commands=creation_commands,
    )
    interaction = RecordingInteraction(interaction_id=993)
    modal = _normal_creation_modal(adapter, interaction)
    modal.gate_numbers._value = "1\n2\n3\n4\n5"
    modal.horse_names._value = "말1\n말2\n말3\n말4"

    asyncio.run(modal.on_submit(interaction))  # type: ignore[arg-type]

    assert creation_commands.calls == []
    assert "non-empty line 개수가 같아야 합니다" in interaction.edits[0]["content"]


@pytest.mark.parametrize(
    ("gate_numbers", "expected"),
    [
        ("1\n2\nthree\n4\n5", "양의 정수"),
        ("1\n2\n3\n4\n4", "중복"),
    ],
)
def test_normal_creation_modal_rejects_invalid_or_duplicate_gate_without_write(
    gate_numbers: str,
    expected: str,
) -> None:
    creation_commands = RecordingCreationCommands(result=_created_round())
    adapter = _adapter(RecordingQueries(), creation_commands=creation_commands)
    interaction = RecordingInteraction()
    modal = _normal_creation_modal(adapter, interaction)
    modal.gate_numbers._value = gate_numbers

    asyncio.run(modal.on_submit(interaction))  # type: ignore[arg-type]

    assert creation_commands.calls == []
    assert expected in interaction.edits[0]["content"]


def test_normal_creation_modal_rejects_draft_that_cannot_fit_a_bounded_preview() -> None:
    creation_commands = RecordingCreationCommands(result=_created_round())
    adapter = _adapter(RecordingQueries(), creation_commands=creation_commands)
    interaction = RecordingInteraction()
    modal = _normal_creation_modal(adapter, interaction)
    modal.gate_numbers._value = f"{'1' * 3600}\n2\n3\n4\n5"

    asyncio.run(modal.on_submit(interaction))  # type: ignore[arg-type]

    assert creation_commands.calls == []
    assert "Discord 표시 한도" in interaction.edits[0]["content"]


def test_normal_creation_entry_correction_is_prefilled_and_updates_only_local_draft() -> None:
    creation_commands = RecordingCreationCommands(result=_created_round())
    creation_queries = RecordingCreationQueries()
    authorization = RecordingAuthorization()
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        creation_queries=creation_queries,
        creation_commands=creation_commands,
        authorization=authorization,
        preparation=preparation,
    )
    opening = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(opening)
    draft = _normal_creation_draft()
    source_view = Win5NormalRoundCreationPreviewView(
        adapter=adapter,
        context=context,
        draft=draft,
    )
    select = _layout_items(source_view, discord.ui.Select)[0]
    selected = next(entry for entry in draft.entries if entry.gate_number == 2)
    select._values = [str(selected.row_id)]
    select_interaction = RecordingInteraction(interaction_id=700)

    asyncio.run(select.callback(select_interaction))  # type: ignore[attr-defined,arg-type]

    assert authorization.calls == [(select_interaction, "win5.staff.round")]
    modal = select_interaction.response.modals[0]
    assert isinstance(modal, Win5NormalRoundEntryCorrectionModal)
    assert modal.gate_number.default == "2"
    assert modal.horse_name.default == "말 2 @everyone"

    modal.gate_number._value = "9"
    modal.horse_name._value = "정정 말 @everyone"
    correction_interaction = RecordingInteraction(interaction_id=701)
    asyncio.run(modal.on_submit(correction_interaction))  # type: ignore[arg-type]

    assert preparation.calls == [(correction_interaction, "win5.staff.round", True)]
    assert creation_queries.calls == []
    assert creation_commands.calls == []
    replacement = correction_interaction.edits[0]["view"]
    assert isinstance(replacement, Win5NormalRoundCreationPreviewView)
    assert (9, "정정 말 @everyone") in tuple(
        (entry.gate_number, entry.horse_name) for entry in replacement.draft.entries
    )
    assert all(entry.gate_number != 2 for entry in replacement.draft.entries)
    assert "adapter-local draft에 반영" in _layout_text(replacement)
    assert "@everyone" not in _layout_text(replacement)


def test_normal_creation_entry_correction_rejects_duplicate_gate_and_keeps_draft() -> None:
    creation_commands = RecordingCreationCommands(result=_created_round())
    adapter = _adapter(RecordingQueries(), creation_commands=creation_commands)
    opening = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(opening)
    draft = _normal_creation_draft()
    source_view = Win5NormalRoundCreationPreviewView(
        adapter=adapter,
        context=context,
        draft=draft,
    )
    entry = next(candidate for candidate in draft.entries if candidate.gate_number == 2)
    modal = Win5NormalRoundEntryCorrectionModal(
        adapter=adapter,
        context=context,
        draft=draft,
        entry=entry,
        source_view=source_view,
    )
    modal.gate_number._value = "1"
    modal.horse_name._value = "중복 게이트"
    interaction = RecordingInteraction()

    asyncio.run(modal.on_submit(interaction))  # type: ignore[arg-type]

    assert creation_commands.calls == []
    replacement = interaction.edits[0]["view"]
    assert isinstance(replacement, Win5NormalRoundCreationPreviewView)
    assert replacement.draft == draft
    assert "게이트 번호는 중복될 수 없습니다" in _layout_text(replacement)


def test_normal_creation_full_reentry_prefills_then_replaces_complete_local_draft() -> None:
    creation_commands = RecordingCreationCommands(result=_created_round())
    adapter = _adapter(RecordingQueries(), creation_commands=creation_commands)
    opening = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(opening)
    source_view = Win5NormalRoundCreationPreviewView(
        adapter=adapter,
        context=context,
        draft=_normal_creation_draft(),
    )
    interaction = RecordingInteraction(interaction_id=710)

    asyncio.run(_layout_button(source_view, label="전체 다시 입력").callback(interaction))  # type: ignore[arg-type]

    modal = interaction.response.modals[0]
    assert isinstance(modal, Win5NormalRoundCreationModal)
    assert modal.round_name.default == "제3회 @everyone 아리마 기념"
    assert modal.gate_numbers.default == "1\n2\n3\n4\n5"
    assert modal.horse_names.default == "말 1 @everyone\n말 2 @everyone\n말 3 @everyone\n말 4 @everyone\n말 5 @everyone"

    modal.round_name._value = "새 라운드"
    modal.race_name._value = "새 Race"
    modal.race_schedule._value = ""
    modal.gate_numbers._value = "10\n20\n30\n40\n50"
    modal.horse_names._value = "가\n나\n다\n라\n마"
    submit = RecordingInteraction(interaction_id=711)
    asyncio.run(modal.on_submit(submit))  # type: ignore[arg-type]

    assert creation_commands.calls == []
    replacement = submit.edits[0]["view"]
    assert isinstance(replacement, Win5NormalRoundCreationPreviewView)
    assert replacement.draft.round_name == "새 라운드"
    assert replacement.draft.race_name == "새 Race"
    assert replacement.draft.scheduled_at is None
    assert tuple(entry.gate_number for entry in replacement.draft.entries) == (10, 20, 30, 40, 50)


def test_normal_creation_pagination_preserves_all_rows_and_gates_final_confirm() -> None:
    creation_commands = RecordingCreationCommands(result=_created_round())
    adapter = _adapter(RecordingQueries(), creation_commands=creation_commands)
    opening = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(opening)
    draft = _normal_creation_draft(count=26)
    source_view = Win5NormalRoundCreationPreviewView(
        adapter=adapter,
        context=context,
        draft=draft,
    )

    assert _layout_button(source_view, label="생성 확정").disabled is True
    assert source_view.total_children_count <= 40
    assert len(_layout_text(source_view)) <= 4000
    assert len(_layout_items(source_view, discord.ui.Select)[0].options) == 20
    interaction = RecordingInteraction(interaction_id=720)
    stop_calls, source_stopped_at_edit = _track_stop_before_edit(source_view, interaction)
    asyncio.run(_layout_button(source_view, label="다음").callback(interaction))  # type: ignore[arg-type]

    assert source_stopped_at_edit == [True]
    assert stop_calls == [True]
    replacement = interaction.response.edits[0]["view"]
    assert isinstance(replacement, Win5NormalRoundCreationPreviewView)
    assert len(replacement.draft.entries) == 26
    assert replacement.total_children_count <= 40
    assert len(_layout_text(replacement)) <= 4000
    assert replacement.draft.page == 1
    assert replacement.draft.all_pages_reviewed
    assert len(_layout_items(replacement, discord.ui.Select)[0].options) == 6
    assert "페이지 2/2 (21~26)" in _layout_text(replacement)
    assert "26 · 말 26" in _layout_text(replacement)
    assert _layout_button(replacement, label="생성 확정").disabled is False
    assert creation_commands.calls == []


def test_normal_creation_page_transition_failure_stops_source_and_prompts_reopen() -> None:
    adapter = _adapter(RecordingQueries())
    opening = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(opening)
    source_view = Win5NormalRoundCreationPreviewView(
        adapter=adapter,
        context=context,
        draft=_normal_creation_draft(count=26),
    )
    interaction = RecordingInteraction(interaction_id=721)
    stop_calls, source_stopped_at_edit = _track_stop_before_edit(source_view, interaction, fail=True)

    asyncio.run(_layout_button(source_view, label="다음").callback(interaction))  # type: ignore[arg-type]

    assert source_stopped_at_edit == [True]
    assert stop_calls == [True]
    assert interaction.response.messages[-1][0] == (
        "WIN5 라운드 생성 화면을 갱신하지 못했습니다. `/win5 staff round`를 다시 열어 주세요."
    )


def test_normal_creation_deferred_replacement_failure_stops_source_and_uses_followup() -> None:
    adapter = _adapter(RecordingQueries())
    opening = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(opening)
    draft = _normal_creation_draft()
    source_view = Win5NormalRoundCreationPreviewView(
        adapter=adapter,
        context=context,
        draft=draft,
    )
    interaction = RecordingInteraction(interaction_id=722)
    interaction.response.done = True
    stop_calls, source_stopped_at_edit = _track_stop_before_edit(
        source_view,
        interaction,
        deferred=True,
        fail=True,
    )

    asyncio.run(
        adapter._edit_deferred_normal_creation_layout(  # noqa: SLF001
            interaction,  # type: ignore[arg-type]
            context=context,
            draft=draft,
            source_view=source_view,
        )
    )

    assert source_stopped_at_edit == [True]
    assert stop_calls == [True]
    assert interaction.followup.messages[-1][0] == (
        "WIN5 라운드 생성 화면을 갱신하지 못했습니다. `/win5 staff round`를 다시 열어 주세요."
    )


def test_normal_creation_final_confirm_calls_existing_command_once_with_final_interaction_key() -> None:
    creation_commands = RecordingCreationCommands(result=_created_round())
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        creation_commands=creation_commands,
        preparation=preparation,
    )
    opening = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(opening)
    source_view = Win5NormalRoundCreationPreviewView(
        adapter=adapter,
        context=context,
        draft=_normal_creation_draft(),
    )
    interaction = RecordingInteraction(interaction_id=991)
    event_loop_thread_id = get_ident()

    asyncio.run(_layout_button(source_view, label="생성 확정").callback(interaction))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "win5.staff.round", True)]
    command, worker_thread_id = creation_commands.calls[0]
    assert worker_thread_id != event_loop_thread_id
    assert command.season_id == 7
    assert command.round_type == Win5RoundType.NORMAL
    assert command.idempotency_key == "win5-normal-round-create:991"
    assert command.actor_discord_user_id == "123"
    assert command.guild_id == "987"
    assert command.correlation_id == "991"
    assert tuple((entry.gate_number, entry.name) for entry in command.races[0].entries) == tuple(
        (entry.gate_number, entry.horse_name) for entry in source_view.draft.entries
    )
    receipt = interaction.edits[0]["view"]
    assert isinstance(receipt, discord.ui.LayoutView)
    assert "WIN5 라운드 생성 완료" in _layout_text(receipt)
    assert "2026-08-30 15:30 KST" in _layout_text(receipt)
    assert "@everyone" not in _layout_text(receipt)

    duplicate = RecordingInteraction(interaction_id=992)
    asyncio.run(
        adapter.confirm_normal_round_creation(  # type: ignore[arg-type]
            duplicate,
            context=context,
            draft=source_view.draft,
            source_view=source_view,
        )
    )
    assert len(creation_commands.calls) == 1
    assert "이미 처리 중이거나 완료" in duplicate.edits[0]["content"]


def test_normal_creation_final_confirm_revalidates_season_in_existing_command() -> None:
    creation_commands = RecordingCreationCommands(
        error=Win5RoundCreationUnavailableError("Season became closed."),
    )
    adapter = _adapter(RecordingQueries(), creation_commands=creation_commands)
    opening = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(opening)
    source_view = Win5NormalRoundCreationPreviewView(
        adapter=adapter,
        context=context,
        draft=_normal_creation_draft(),
    )
    interaction = RecordingInteraction(interaction_id=730)

    asyncio.run(_layout_button(source_view, label="생성 확정").callback(interaction))  # type: ignore[arg-type]

    assert len(creation_commands.calls) == 1
    terminal = interaction.edits[0]["view"]
    assert isinstance(terminal, discord.ui.LayoutView)
    assert "Season이 더 이상 Round 생성 대상이 아닙니다" in _layout_text(terminal)


def test_normal_creation_final_delivery_failure_still_closes_source_after_command() -> None:
    creation_commands = RecordingCreationCommands(result=_created_round())
    adapter = _adapter(RecordingQueries(), creation_commands=creation_commands)
    opening = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(opening)
    source_view = Win5NormalRoundCreationPreviewView(
        adapter=adapter,
        context=context,
        draft=_normal_creation_draft(),
    )
    interaction = RecordingInteraction(interaction_id=731)
    stop_calls = _track_stop(source_view)

    async def fail_delivery(**_kwargs: object) -> None:
        raise RuntimeError("Discord terminal edit failed")

    interaction.edit_original_response = fail_delivery  # type: ignore[method-assign]

    asyncio.run(_layout_button(source_view, label="생성 확정").callback(interaction))  # type: ignore[arg-type]

    assert len(creation_commands.calls) == 1
    assert stop_calls == [True]


def test_normal_creation_cancel_discards_local_draft_without_write() -> None:
    creation_commands = RecordingCreationCommands(result=_created_round())
    adapter = _adapter(RecordingQueries(), creation_commands=creation_commands)
    opening = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(opening)
    source_view = Win5NormalRoundCreationPreviewView(
        adapter=adapter,
        context=context,
        draft=_normal_creation_draft(),
    )
    interaction = RecordingInteraction(interaction_id=740)
    stop_calls, source_stopped_at_edit = _track_stop_before_edit(source_view, interaction)

    asyncio.run(_layout_button(source_view, label="취소").callback(interaction))  # type: ignore[arg-type]

    assert creation_commands.calls == []
    assert source_stopped_at_edit == [True]
    assert stop_calls == [True]
    terminal = interaction.response.edits[0]["view"]
    assert isinstance(terminal, discord.ui.LayoutView)
    assert "생성을 취소했습니다" in _layout_text(terminal)


def test_special_creation_modal_preserves_non_empty_race_line_order() -> None:
    creation_commands = RecordingCreationCommands(result=_created_round(round_type=Win5RoundType.SPECIAL))
    adapter = _adapter(
        RecordingQueries(),
        creation_commands=creation_commands,
    )
    interaction = RecordingInteraction(interaction_id=992)
    modal = Win5SpecialRoundCreationModal(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(interaction),
        season=_creation_season(),
    )
    modal.round_name._value = "여름 특별전"
    modal.race_names._value = "  삿포로 1R\n\n니가타 2R  "
    modal.reason._value = ""

    asyncio.run(modal.on_submit(interaction))  # type: ignore[arg-type]

    command, _ = creation_commands.calls[0]
    assert command.round_type == Win5RoundType.SPECIAL
    assert tuple(race.name for race in command.races) == ("삿포로 1R", "니가타 2R")
    assert all(race.scheduled_at is None for race in command.races)
    assert command.idempotency_key == "win5-special-round-create:992"


def test_creation_modal_context_mismatch_rejects_before_prepare_or_command() -> None:
    creation_commands = RecordingCreationCommands(result=_created_round())
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        creation_commands=creation_commands,
        preparation=preparation,
    )
    original = RecordingInteraction()
    other = RecordingInteraction(user_id=999)
    modal = Win5NormalRoundCreationModal(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(original),
        season=_creation_season(),
    )
    modal.round_name._value = "라운드"
    modal.race_name._value = "Race"
    modal.race_schedule._value = ""
    modal.gate_numbers._value = "1\n2\n3\n4\n5"
    modal.horse_names._value = "말1\n말2\n말3\n말4\n말5"

    asyncio.run(modal.on_submit(other))  # type: ignore[arg-type]

    assert preparation.calls == []
    assert creation_commands.calls == []
    assert "연 사용자와 서버·채널" in other.response.messages[0][0]


def test_normal_creation_final_confirm_context_mismatch_rejects_before_prepare_or_command() -> None:
    creation_commands = RecordingCreationCommands(result=_created_round())
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        creation_commands=creation_commands,
        preparation=preparation,
    )
    opening = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(opening)
    source_view = Win5NormalRoundCreationPreviewView(
        adapter=adapter,
        context=context,
        draft=_normal_creation_draft(),
    )
    other = RecordingInteraction(user_id=999)

    asyncio.run(_layout_button(source_view, label="생성 확정").callback(other))  # type: ignore[arg-type]

    assert preparation.calls == []
    assert creation_commands.calls == []
    assert "연 사용자와 서버·채널" in other.response.messages[0][0]


def test_round_creation_parsers_and_success_formatter_keep_boundaries_explicit() -> None:
    assert parse_special_round_race_names("Race A\n\n Race B ") == ("Race A", "Race B")
    assert parse_optional_normal_race_schedule("") is None
    assert parse_optional_normal_race_schedule("2026-08-30 15:30") == datetime(
        2026,
        8,
        30,
        6,
        30,
        tzinfo=UTC,
    )
    assert tuple(
        (entry.gate_number, entry.name)
        for entry in parse_normal_round_entries(
            "8\n\n1\n7\n2\n4",
            "라이스 샤워\n\n스페셜 위크\n메지로 맥퀸\n사일런스 스즈카\n토카이 테이오",
        )
    ) == (
        (1, "스페셜 위크"),
        (2, "사일런스 스즈카"),
        (4, "토카이 테이오"),
        (7, "메지로 맥퀸"),
        (8, "라이스 샤워"),
    )
    with pytest.raises(ValueError, match="YYYY-MM-DD HH:MM"):
        parse_optional_normal_race_schedule("2026-08-30")
    with pytest.raises(ValueError, match="non-empty line 개수가 같아야"):
        parse_normal_round_entries("1\n2\n3\n4\n5", "말1\n말2")
    with pytest.raises(ValueError, match="중복"):
        parse_normal_round_entries("1\n2\n3\n4\n4", "말1\n말2\n말3\n말4\n말5")
    with pytest.raises(ValueError, match="최소 5개"):
        build_normal_round_creation_draft(
            season=_creation_season(),
            round_name="Round",
            race_name="Race",
            race_schedule="",
            gate_numbers="1\n2\n3\n4",
            horse_names="말1\n말2\n말3\n말4",
        )
    with pytest.raises(ValueError, match="양의 정수"):
        parse_normal_round_entries("1|말1\n2\n3\n4\n5", "말1\n말2\n말3\n말4\n말5")
    with pytest.raises(ValueError, match="하나 이상"):
        parse_special_round_race_names("\n  \n")

    normal_message = format_round_creation_success(_created_round())
    assert "@everyone" not in normal_message
    assert "Entry: 5개" in normal_message
    assert "게이트 번호: 1, 2, 4, 7, 8" in normal_message

    special_message = format_round_creation_success(_created_round(round_type=Win5RoundType.SPECIAL))
    assert "@everyone" not in special_message
    assert "Race: 2개" in special_message


def test_composition_root_adds_staff_round_without_running_database_work() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    preparation = RecordingPreparation(allowed=False)
    authorization = RecordingAuthorization(allowed=False)
    try:
        group = compose_win5_command_group(
            runtime,
            prepare_command=preparation,  # type: ignore[arg-type]
            authorize_autocomplete=authorization,  # type: ignore[arg-type]
            authorize_interaction=authorization,  # type: ignore[arg-type]
        )
    finally:
        runtime.dispose()

    assert [command.name for command in group.commands] == [
        "info",
        "rounds",
        "submit",
        "special-submit",
        "submissions",
        "cancel",
        "standings",
        "staff",
    ]
    staff = group.get_command("staff")
    assert isinstance(staff, app_commands.Group)
    assert [command.name for command in staff.commands] == ["round", "season"]
