"""Discord adapter tests for WIN5 staff result and scoring workflows."""

from __future__ import annotations

from win5_staff_discord_test_support import (
    UTC,
    CancelSpecialWin5Round,
    RecordingAuthorization,
    RecordingCommands,
    RecordingInteraction,
    RecordingPreparation,
    RecordingQueries,
    RecordingScoringCommands,
    RecordingSpecialCommands,
    RecordingSpecialScoringCommands,
    RecordingSpecialVoidCommands,
    RecordingSpecialVoidQueries,
    SetSpecialWin5RaceVoid,
    SimpleNamespace,
    Win5NormalResultConfirmView,
    Win5NormalResultModal,
    Win5NormalResultPlacementInput,
    Win5NormalResultTargetMode,
    Win5NormalResultVersionConflictError,
    Win5NormalScoringConfirmView,
    Win5NormalScoringPreview,
    Win5NormalScoringTargetView,
    Win5NormalScoringWalletUnavailableError,
    Win5SpecialResultConfirmView,
    Win5SpecialResultEditorView,
    Win5SpecialResultModal,
    Win5SpecialResultPreview,
    Win5SpecialResultRace,
    Win5SpecialResultTarget,
    Win5SpecialResultTargetMode,
    Win5SpecialResultTargetView,
    Win5SpecialResultVersionConflictError,
    Win5SpecialResultWinnerInput,
    Win5SpecialRoundCancellationConfirmView,
    Win5SpecialRoundCancellationPreview,
    Win5SpecialRoundCancellationReasonModal,
    Win5SpecialRoundCancellationTargetView,
    Win5SpecialScoringConfirmView,
    Win5SpecialScoringPreview,
    Win5SpecialScoringTargetView,
    Win5SpecialVoidConfirmView,
    Win5SpecialVoidPreview,
    Win5SpecialVoidRaceView,
    Win5SpecialVoidReasonModal,
    Win5SpecialVoidTargetView,
    Win5StaffInteractionContext,
    Win5StaffRoundActionView,
    Win5StaffSpecialVoidRace,
    Win5StaffSpecialVoidTarget,
    _adapter,
    _cancelled_special_round,
    _choices,
    _saved_result,
    _saved_special_result,
    _scored_result,
    _scored_special_result,
    _special_choices,
    _special_target,
    _special_void_choices,
    _special_void_target,
    _target,
    _updated_special_void,
    asyncio,
    datetime,
    discord,
    fingerprint_special_void_state,
    format_normal_result_preview,
    format_normal_scoring_preview,
    format_special_result_editor,
    format_special_result_preview,
    format_special_round_cancellation_preview,
    format_special_scoring_preview,
    format_special_void_preview,
    get_ident,
    parse_normal_result_gate_numbers,
    parse_special_result_gate_numbers,
    pytest,
)


def _track_stop(view: discord.ui.View) -> list[bool]:
    original_stop = view.stop
    calls: list[bool] = []

    def record_stop() -> None:
        calls.append(True)
        original_stop()

    view.stop = record_stop  # type: ignore[method-assign]
    return calls


def _track_stop_before_edit(
    view: discord.ui.View,
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


def test_normal_result_action_closes_source_before_target_replacement() -> None:
    queries = RecordingQueries(choices=_choices(mode=Win5NormalResultTargetMode.ENTRY))
    adapter = _adapter(queries)
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5StaffRoundActionView(adapter=adapter, context=context)
    action = source_view.children[0]
    action._values = [Win5NormalResultTargetMode.ENTRY.value]
    stop_calls, stopped_at_edit = _track_stop_before_edit(source_view, interaction)

    asyncio.run(action.callback(interaction))  # type: ignore[arg-type]

    assert stop_calls == [True]
    assert stopped_at_edit == [True]
    assert isinstance(interaction.response.edits[0]["view"], discord.ui.View)


def test_scoring_action_reuses_complete_result_choices_in_worker() -> None:
    queries = RecordingQueries(choices=_choices(mode=Win5NormalResultTargetMode.CORRECTION))
    authorization = RecordingAuthorization()
    adapter = _adapter(queries, authorization=authorization)
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5StaffRoundActionView(adapter=adapter, context=context)
    select = source_view.children[0]
    select._values = ["normal-scoring"]
    stop_calls, stopped_at_edit = _track_stop_before_edit(source_view, interaction)
    event_loop_thread_id = get_ident()

    asyncio.run(select.callback(interaction))  # type: ignore[arg-type]

    assert authorization.calls == [(interaction, "win5.staff.round")]
    assert queries.list_calls[0][:2] == (Win5NormalResultTargetMode.CORRECTION, 25)
    assert queries.list_calls[0][2] != event_loop_thread_id
    assert stop_calls == [True]
    assert stopped_at_edit == [True]
    target_view = interaction.response.edits[0]["view"]
    assert isinstance(target_view, Win5NormalScoringTargetView)


def test_special_scoring_action_reuses_complete_result_choices_in_worker() -> None:
    queries = RecordingQueries(special_choices=_special_choices(mode=Win5SpecialResultTargetMode.CORRECTION))
    authorization = RecordingAuthorization()
    adapter = _adapter(queries, authorization=authorization)
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5StaffRoundActionView(adapter=adapter, context=context)
    select = source_view.children[0]
    select._values = ["special-scoring"]
    stop_calls, stopped_at_edit = _track_stop_before_edit(source_view, interaction)
    event_loop_thread_id = get_ident()

    asyncio.run(select.callback(interaction))  # type: ignore[arg-type]

    assert authorization.calls == [(interaction, "win5.staff.round")]
    assert queries.special_list_calls[0][:2] == (Win5SpecialResultTargetMode.CORRECTION, 25)
    assert queries.special_list_calls[0][2] != event_loop_thread_id
    assert stop_calls == [True]
    assert stopped_at_edit == [True]
    target_view = interaction.response.edits[0]["view"]
    assert isinstance(target_view, Win5SpecialScoringTargetView)


def test_special_scoring_target_selection_renders_complete_zero_write_preview() -> None:
    target = _special_target()
    queries = RecordingQueries(special_target=target)
    scoring_commands = RecordingSpecialScoringCommands()
    preparation = RecordingPreparation()
    adapter = _adapter(
        queries,
        special_scoring_commands=scoring_commands,
        preparation=preparation,
    )
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5SpecialScoringTargetView(
        adapter=adapter,
        context=context,
        choices=(_special_choices(mode=Win5SpecialResultTargetMode.CORRECTION)[0],),
    )
    select = source_view.children[0]
    select._values = ["21"]
    stop_calls, stopped_at_edit = _track_stop_before_edit(
        source_view,
        interaction,
        deferred=True,
    )
    event_loop_thread_id = get_ident()

    asyncio.run(select.callback(interaction))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "win5.staff.round", True)]
    assert queries.special_get_calls[0][:2] == (21, Win5SpecialResultTargetMode.CORRECTION)
    assert queries.special_get_calls[0][2] != event_loop_thread_id
    assert scoring_commands.calls == []
    assert stop_calls == [True]
    assert stopped_at_edit == [True]
    edit = interaction.edits[0]
    assert edit["content"] == (
        "WIN5 특별 라운드 채점 확인\n"
        "시즌: 2026 @\u200beveryone 하반기\n"
        "라운드: 여름 @\u200beveryone Special\n"
        "권위 결과:\n"
        "- Race 1 @\u200beveryone: 3\n"
        "- Race 2 @\u200beveryone: 7\n"
        "- Race 3 @\u200beveryone: 1\n"
        "확정하면 현재 접수된 제출 전체를 한 batch로 채점하고 적중 Race마다 시즌/TOP1 승점을 각각 1점 반영합니다.\n"
        "서클 포인트는 변경하지 않으며, 채점 완료 후에는 결과를 정정하거나 다시 채점할 수 없습니다."
    )
    assert isinstance(edit["view"], Win5SpecialScoringConfirmView)


def test_scoring_target_selection_reauthorizes_and_renders_zero_write_preview() -> None:
    target = _target()
    queries = RecordingQueries(target=target)
    scoring_commands = RecordingScoringCommands()
    preparation = RecordingPreparation()
    adapter = _adapter(
        queries,
        scoring_commands=scoring_commands,
        preparation=preparation,
    )
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5NormalScoringTargetView(
        adapter=adapter,
        context=context,
        choices=(_choices(mode=Win5NormalResultTargetMode.CORRECTION)[0],),
    )
    select = source_view.children[0]
    select._values = ["11"]
    stop_calls, stopped_at_edit = _track_stop_before_edit(
        source_view,
        interaction,
        deferred=True,
    )
    event_loop_thread_id = get_ident()

    asyncio.run(select.callback(interaction))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "win5.staff.round", True)]
    assert queries.get_calls[0][:2] == (11, Win5NormalResultTargetMode.CORRECTION)
    assert queries.get_calls[0][2] != event_loop_thread_id
    assert scoring_commands.calls == []
    assert stop_calls == [True]
    assert stopped_at_edit == [True]
    edit = interaction.edits[0]
    assert edit["content"] == (
        "WIN5 일반 라운드 채점 확인\n"
        "시즌: 2026 @\u200beveryone 하반기\n"
        "라운드: 제3회 @\u200beveryone 아리마 기념\n"
        "경기: 아리마 \\*\\*기념\\*\\*\n"
        "권위 결과:\n"
        "- 1착: 1 · 말 1 @\u200beveryone\n"
        "- 2착: 2 · 말 2\n"
        "- 3착: 3 · 말 3\n"
        "- 4착: 4 · 말 4\n"
        "- 5착: 5 · 말 5\n"
        "확정하면 현재 접수된 제출 전체를 이 결과로 채점하고 승점과 서클 포인트 보상을 한 번에 반영합니다.\n"
        "채점 완료 후에는 결과를 정정하거나 다시 채점할 수 없습니다."
    )
    assert isinstance(edit["view"], Win5NormalScoringConfirmView)


def test_target_select_opens_text_input_only_bound_modal_without_defer() -> None:
    authorization = RecordingAuthorization()
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        authorization=authorization,
        preparation=preparation,
    )
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    from uma_st2.adapters.discord.win5_staff import Win5NormalResultTargetView

    source_view = Win5NormalResultTargetView(
        adapter=adapter,
        context=context,
        mode=Win5NormalResultTargetMode.CORRECTION,
        choices=(_choices(mode=Win5NormalResultTargetMode.CORRECTION)[0],),
    )
    stop_calls = _track_stop(source_view)
    select = source_view.children[0]
    select._values = ["11"]

    asyncio.run(select.callback(interaction))  # type: ignore[arg-type]

    assert preparation.calls == []
    assert authorization.calls == [(interaction, "win5.staff.round")]
    modal = interaction.response.modals[0]
    assert isinstance(modal, Win5NormalResultModal)
    assert modal.title == "WIN5 일반 결과 정정"
    assert len(modal.children) == 2
    assert all(isinstance(child, discord.ui.TextInput) for child in modal.children)
    assert modal.result_order.required is True
    assert modal.reason.required is False
    assert modal._source_view is source_view
    assert stop_calls == []


def test_modal_preview_reauthorizes_maps_gates_and_renders_before_after_in_worker() -> None:
    target = _target()
    queries = RecordingQueries(target=target)
    preparation = RecordingPreparation()
    adapter = _adapter(queries, preparation=preparation)
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    from uma_st2.adapters.discord.win5_staff import Win5NormalResultTargetView

    source_view = Win5NormalResultTargetView(
        adapter=adapter,
        context=context,
        mode=Win5NormalResultTargetMode.CORRECTION,
        choices=(_choices(mode=Win5NormalResultTargetMode.CORRECTION)[0],),
    )
    modal = Win5NormalResultModal(
        adapter=adapter,
        context=context,
        mode=Win5NormalResultTargetMode.CORRECTION,
        round_id=11,
        source_view=source_view,
    )
    modal.result_order._value = "2-1-3-4-5"
    modal.reason._value = "1착과 2착 정정"
    stop_calls, stopped_at_edit = _track_stop_before_edit(
        source_view,
        interaction,
        deferred=True,
    )
    event_loop_thread_id = get_ident()

    asyncio.run(modal.on_submit(interaction))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "win5.staff.round", True)]
    assert queries.get_calls[0][:2] == (11, Win5NormalResultTargetMode.CORRECTION)
    assert queries.get_calls[0][2] != event_loop_thread_id
    assert stop_calls == [True]
    assert stopped_at_edit == [True]
    edit = interaction.edits[0]
    assert edit["content"] == (
        "WIN5 일반 결과 정정 확인\n"
        "시즌: 2026 @\u200beveryone 하반기\n"
        "라운드: 제3회 @\u200beveryone 아리마 기념\n"
        "경기: 아리마 \\*\\*기념\\*\\*\n"
        "현재 결과:\n"
        "- 1착: 1 · 말 1 @\u200beveryone\n"
        "- 2착: 2 · 말 2\n"
        "- 3착: 3 · 말 3\n"
        "- 4착: 4 · 말 4\n"
        "- 5착: 5 · 말 5\n"
        "저장할 결과:\n"
        "- 1착: 2 · 말 2\n"
        "- 2착: 1 · 말 1 @\u200beveryone\n"
        "- 3착: 3 · 말 3\n"
        "- 4착: 4 · 말 4\n"
        "- 5착: 5 · 말 5\n"
        "운영 메모: 1착과 2착 정정\n"
        "확정하면 이 전체 결과가 권위 결과로 저장됩니다."
    )
    confirm_view = edit["view"]
    assert isinstance(confirm_view, Win5NormalResultConfirmView)


def test_confirmation_uses_final_interaction_id_expected_fingerprint_and_worker_command() -> None:
    target = _target()
    placements = tuple(
        Win5NormalResultPlacementInput(position=position, race_entry_id=entry_id)
        for position, entry_id in enumerate((102, 101, 103, 104, 105), start=1)
    )
    preview = SimpleNamespace(target=target, placements=placements, reason="정정 확인")
    commands = RecordingCommands(result=_saved_result(placements))
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        commands,
        preparation=preparation,
    )
    interaction = RecordingInteraction(interaction_id=999)
    context = Win5StaffInteractionContext.from_interaction(interaction)
    view = Win5NormalResultConfirmView(
        adapter=adapter,
        context=context,
        preview=preview,  # type: ignore[arg-type]
    )
    stop_calls = _track_stop(view)
    original_save_result = commands.save_result

    def save_result_after_view_closed(command: object) -> object:
        assert stop_calls == [True]
        return original_save_result(command)  # type: ignore[arg-type]

    commands.save_result = save_result_after_view_closed  # type: ignore[method-assign]
    event_loop_thread_id = get_ident()

    asyncio.run(view.children[0].callback(interaction))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "win5.staff.round", True)]
    command, worker_thread_id = commands.calls[0]
    assert worker_thread_id != event_loop_thread_id
    assert command.round_id == 11
    assert command.placements == placements
    assert command.expected_result_fingerprint == target.result_fingerprint
    assert command.idempotency_key == "win5-normal-result:999"
    assert command.actor_discord_user_id == "123"
    assert command.guild_id == "987"
    assert command.correlation_id == "999"
    assert command.reason == "정정 확인"
    assert interaction.edits[0]["content"] == (
        "WIN5 일반 결과 정정 완료\n라운드: 제3회 @\u200beveryone 아리마 기념\n"
        "경기: 아리마 \\*\\*기념\\*\\*\n1~5착: 2-1-3-4-5"
    )
    assert interaction.edits[0]["view"] is None
    assert stop_calls == [True]


def test_normal_result_confirmation_is_claimed_once() -> None:
    target = _target()
    placements = tuple(
        Win5NormalResultPlacementInput(position=position, race_entry_id=100 + position) for position in range(1, 6)
    )
    commands = RecordingCommands(result=_saved_result(placements))
    adapter = _adapter(RecordingQueries(), commands)
    opening = RecordingInteraction(interaction_id=998)
    view = Win5NormalResultConfirmView(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(opening),
        preview=SimpleNamespace(target=target, placements=placements, reason=None),  # type: ignore[arg-type]
    )
    retry = RecordingInteraction(interaction_id=999)

    asyncio.run(view.children[0].callback(opening))  # type: ignore[arg-type]
    asyncio.run(view.children[0].callback(retry))  # type: ignore[arg-type]

    assert len(commands.calls) == 1
    assert "이미 처리 중이거나 완료" in retry.response.messages[0][0]


def test_normal_result_transition_failure_closes_source_and_sends_reopen_notice() -> None:
    adapter = _adapter(RecordingQueries(choices=_choices()))
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5StaffRoundActionView(adapter=adapter, context=context)
    stop_calls, stopped_at_edit = _track_stop_before_edit(source_view, interaction, fail=True)

    asyncio.run(
        adapter.show_normal_result_targets(
            interaction,  # type: ignore[arg-type]
            context=context,
            mode=Win5NormalResultTargetMode.ENTRY,
            source_view=source_view,
        )
    )

    assert stop_calls == [True]
    assert stopped_at_edit == [True]
    assert "`/win5 staff round`를 다시 열어" in interaction.response.messages[0][0]


def test_normal_result_cancel_failure_still_closes_source() -> None:
    adapter = _adapter(RecordingQueries())
    interaction = RecordingInteraction()
    view = Win5NormalResultConfirmView(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(interaction),
        preview=SimpleNamespace(target=_target(), placements=(), reason=None),  # type: ignore[arg-type]
    )
    stop_calls, stopped_at_edit = _track_stop_before_edit(view, interaction, fail=True)

    asyncio.run(view.children[1].callback(interaction))  # type: ignore[arg-type]

    assert stop_calls == [True]
    assert stopped_at_edit == [True]


def test_special_result_action_uses_worker_query_and_bounded_round_choices() -> None:
    queries = RecordingQueries(special_choices=_special_choices(void_count=1))
    authorization = RecordingAuthorization()
    adapter = _adapter(queries, authorization=authorization)
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5StaffRoundActionView(adapter=adapter, context=context)
    select = source_view.children[0]
    select._values = ["special-result-entry"]
    stop_calls, stopped_at_edit = _track_stop_before_edit(source_view, interaction)
    event_loop_thread_id = get_ident()

    asyncio.run(select.callback(interaction))  # type: ignore[arg-type]

    assert authorization.calls == [(interaction, "win5.staff.round")]
    assert queries.special_list_calls[0][:2] == (Win5SpecialResultTargetMode.ENTRY, 25)
    assert queries.special_list_calls[0][2] != event_loop_thread_id
    assert stop_calls == [True]
    assert stopped_at_edit == [True]
    target_view = interaction.response.edits[0]["view"]
    assert isinstance(target_view, Win5SpecialResultTargetView)
    target_select = target_view.children[0]
    assert [option.value for option in target_select.options] == ["21", "22"]
    assert [option.label for option in target_select.options] == [
        "같은 특별 라운드 @\u200beveryone · ID 21",
        "같은 특별 라운드 @\u200beveryone · ID 22",
    ]
    assert [option.description for option in target_select.options] == [
        "Race 3개 · VOID 1개",
        "Race 3개 · VOID 1개",
    ]


def test_special_result_target_shows_current_state_before_text_input_only_modal() -> None:
    target = _special_target(void_ids=(102,))
    authorization = RecordingAuthorization()
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(special_target=target),
        authorization=authorization,
        preparation=preparation,
    )
    select_interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(select_interaction)
    view = Win5SpecialResultTargetView(
        adapter=adapter,
        context=context,
        mode=Win5SpecialResultTargetMode.CORRECTION,
        choices=(_special_choices(mode=Win5SpecialResultTargetMode.CORRECTION)[0],),
    )
    select = view.children[0]
    select._values = ["21"]
    target_stop_calls, target_stopped_at_edit = _track_stop_before_edit(view, select_interaction)

    asyncio.run(select.callback(select_interaction))  # type: ignore[arg-type]

    assert preparation.calls == []
    assert authorization.calls == [(select_interaction, "win5.staff.round")]
    assert target_stop_calls == [True]
    assert target_stopped_at_edit == [True]
    editor_view = select_interaction.response.edits[0]["view"]
    assert isinstance(editor_view, Win5SpecialResultEditorView)
    assert select_interaction.response.edits[0]["content"] == format_special_result_editor(target)
    assert "Race 2 @\u200beveryone: VOID — 공식 취소 @\u200beveryone" in format_special_result_editor(target)

    modal_interaction = RecordingInteraction()
    editor_stop_calls = _track_stop(editor_view)
    asyncio.run(editor_view.children[0].callback(modal_interaction))  # type: ignore[arg-type]

    assert authorization.calls[-1] == (modal_interaction, "win5.staff.round")
    modal = modal_interaction.response.modals[0]
    assert isinstance(modal, Win5SpecialResultModal)
    assert modal.title == "WIN5 특별 결과 정정"
    assert len(modal.children) == 2
    assert all(isinstance(child, discord.ui.TextInput) for child in modal.children)
    assert modal.winner_gates.default == "3\n1"
    assert modal._source_view is editor_view
    assert editor_stop_calls == []


def test_special_result_modal_requeries_race_order_and_renders_complete_preview() -> None:
    target = _special_target(void_ids=(102,))
    queries = RecordingQueries(special_target=target)
    preparation = RecordingPreparation()
    adapter = _adapter(queries, preparation=preparation)
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5SpecialResultEditorView(
        adapter=adapter,
        context=context,
        target=target,
    )
    modal = Win5SpecialResultModal(
        adapter=adapter,
        context=context,
        target=target,
        source_view=source_view,
    )
    modal.winner_gates._value = "1\n3"
    modal.reason._value = "전체 확인"
    stop_calls, stopped_at_edit = _track_stop_before_edit(
        source_view,
        interaction,
        deferred=True,
    )
    event_loop_thread_id = get_ident()

    asyncio.run(modal.on_submit(interaction))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "win5.staff.round", True)]
    assert queries.special_get_calls[0][:2] == (21, Win5SpecialResultTargetMode.CORRECTION)
    assert queries.special_get_calls[0][2] != event_loop_thread_id
    assert stop_calls == [True]
    assert stopped_at_edit == [True]
    edit = interaction.edits[0]
    assert edit["content"] == (
        "WIN5 특별 결과 정정 확인\n"
        "시즌: 2026 @\u200beveryone 하반기\n"
        "라운드: 여름 @\u200beveryone Special\n"
        "현재 결과:\n"
        "- Race 1 @\u200beveryone: 3\n"
        "- Race 2 @\u200beveryone: VOID — 공식 취소 @\u200beveryone\n"
        "- Race 3 @\u200beveryone: 1\n"
        "저장할 결과:\n"
        "- Race 1 @\u200beveryone: 1 · 참고 1 @\u200beveryone\n"
        "- Race 2 @\u200beveryone: VOID — 공식 취소 @\u200beveryone\n"
        "- Race 3 @\u200beveryone: 3 · 참고 3 @\u200beveryone\n"
        "운영 메모: 전체 확인\n"
        "확정하면 모든 non-void Race의 우승 게이트가 하나의 권위 결과 묶음으로 저장됩니다."
    )
    assert isinstance(edit["view"], Win5SpecialResultConfirmView)


def test_special_result_confirmation_uses_final_interaction_and_expected_fingerprint() -> None:
    target = _special_target()
    winners = tuple(
        Win5SpecialResultWinnerInput(race_id=race_id, gate_number=gate_number)
        for race_id, gate_number in ((101, 1), (102, 9), (103, 3))
    )
    preview = Win5SpecialResultPreview(target=target, winners=winners, reason="정정 확인")
    special_commands = RecordingSpecialCommands(result=_saved_special_result(winners))
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        special_commands=special_commands,
        preparation=preparation,
    )
    interaction = RecordingInteraction(interaction_id=999)
    view = Win5SpecialResultConfirmView(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(interaction),
        preview=preview,
    )
    stop_calls = _track_stop(view)
    original_save_result = special_commands.save_result

    def save_result_after_view_closed(command: object) -> object:
        assert stop_calls == [True]
        return original_save_result(command)  # type: ignore[arg-type]

    special_commands.save_result = save_result_after_view_closed  # type: ignore[method-assign]
    event_loop_thread_id = get_ident()

    asyncio.run(view.children[0].callback(interaction))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "win5.staff.round", True)]
    command, worker_thread_id = special_commands.calls[0]
    assert worker_thread_id != event_loop_thread_id
    assert command.round_id == 21
    assert command.winners == winners
    assert command.expected_result_fingerprint == target.result_fingerprint
    assert command.idempotency_key == "win5-special-result:999"
    assert command.actor_discord_user_id == "123"
    assert command.guild_id == "987"
    assert command.correlation_id == "999"
    assert command.reason == "정정 확인"
    assert interaction.edits[0]["content"] == (
        "WIN5 특별 결과 정정 완료\n라운드: 여름 @\u200beveryone Special\n"
        "non-void Race 순서별 우승 게이트: 1-9-3\nVOID Race: 0개"
    )
    assert interaction.edits[0]["view"] is None
    assert stop_calls == [True]


def test_special_result_confirmation_is_claimed_once() -> None:
    target = _special_target()
    winners = tuple(
        Win5SpecialResultWinnerInput(race_id=race_id, gate_number=gate_number)
        for race_id, gate_number in ((101, 1), (102, 9), (103, 3))
    )
    special_commands = RecordingSpecialCommands(result=_saved_special_result(winners))
    adapter = _adapter(RecordingQueries(), special_commands=special_commands)
    opening = RecordingInteraction(interaction_id=998)
    view = Win5SpecialResultConfirmView(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(opening),
        preview=Win5SpecialResultPreview(target=target, winners=winners, reason=None),
    )
    retry = RecordingInteraction(interaction_id=999)

    asyncio.run(view.children[0].callback(opening))  # type: ignore[arg-type]
    asyncio.run(view.children[0].callback(retry))  # type: ignore[arg-type]

    assert len(special_commands.calls) == 1
    assert "이미 처리 중이거나 완료" in retry.response.messages[0][0]


def test_special_result_transition_failure_closes_source_and_sends_reopen_notice() -> None:
    adapter = _adapter(RecordingQueries(special_choices=_special_choices()))
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5StaffRoundActionView(adapter=adapter, context=context)
    stop_calls, stopped_at_edit = _track_stop_before_edit(source_view, interaction, fail=True)

    asyncio.run(
        adapter.show_special_result_targets(
            interaction,  # type: ignore[arg-type]
            context=context,
            mode=Win5SpecialResultTargetMode.ENTRY,
            source_view=source_view,
        )
    )

    assert stop_calls == [True]
    assert stopped_at_edit == [True]
    assert "`/win5 staff round`를 다시 열어" in interaction.response.messages[0][0]


def test_special_result_cancel_failure_still_closes_source() -> None:
    adapter = _adapter(RecordingQueries())
    interaction = RecordingInteraction()
    view = Win5SpecialResultConfirmView(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(interaction),
        preview=Win5SpecialResultPreview(target=_special_target(), winners=(), reason=None),
    )
    stop_calls, stopped_at_edit = _track_stop_before_edit(view, interaction, fail=True)

    asyncio.run(view.children[1].callback(interaction))  # type: ignore[arg-type]

    assert stop_calls == [True]
    assert stopped_at_edit == [True]


def test_scoring_confirmation_uses_final_interaction_id_and_atomic_command() -> None:
    preview = Win5NormalScoringPreview(target=_target())
    scoring_commands = RecordingScoringCommands(result=_scored_result())
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        scoring_commands=scoring_commands,
        preparation=preparation,
    )
    interaction = RecordingInteraction(interaction_id=999)
    view = Win5NormalScoringConfirmView(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(interaction),
        preview=preview,
    )
    stop_calls = _track_stop(view)
    original_score_round = scoring_commands.score_round

    def score_round_after_view_closed(command: object) -> object:
        assert stop_calls == [True]
        return original_score_round(command)  # type: ignore[arg-type]

    scoring_commands.score_round = score_round_after_view_closed  # type: ignore[method-assign]
    event_loop_thread_id = get_ident()

    asyncio.run(view.children[0].callback(interaction))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "win5.staff.round", True)]
    command, worker_thread_id = scoring_commands.calls[0]
    assert worker_thread_id != event_loop_thread_id
    assert command.round_id == 11
    assert command.idempotency_key == "win5-normal-scoring:999"
    assert command.actor_discord_user_id == "123"
    assert command.guild_id == "987"
    assert command.correlation_id == "999"
    assert command.reason is None
    assert interaction.edits[0]["content"] == (
        "WIN5 일반 라운드 채점 완료\n"
        "라운드: 제3회 @\u200beveryone 아리마 기념\n"
        "경기: 아리마 \\*\\*기념\\*\\*\n"
        "채점 제출: 1건\n"
        "시즌 승점 합계: 4점\n"
        "TOP1 승점 합계: 0점\n"
        "서클 포인트 지급 합계: 10"
    )
    assert interaction.edits[0]["view"] is None
    assert stop_calls == [True]


def test_normal_scoring_confirmation_is_claimed_once() -> None:
    scoring_commands = RecordingScoringCommands(result=_scored_result())
    adapter = _adapter(RecordingQueries(), scoring_commands=scoring_commands)
    opening = RecordingInteraction(interaction_id=998)
    view = Win5NormalScoringConfirmView(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(opening),
        preview=Win5NormalScoringPreview(target=_target()),
    )
    retry = RecordingInteraction(interaction_id=999)

    asyncio.run(view.children[0].callback(opening))  # type: ignore[arg-type]
    asyncio.run(view.children[0].callback(retry))  # type: ignore[arg-type]

    assert len(scoring_commands.calls) == 1
    assert "이미 처리 중이거나 완료" in retry.response.messages[0][0]


def test_normal_scoring_transition_failure_closes_source_and_sends_reopen_notice() -> None:
    adapter = _adapter(RecordingQueries(choices=_choices(mode=Win5NormalResultTargetMode.CORRECTION)))
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5StaffRoundActionView(adapter=adapter, context=context)
    stop_calls, stopped_at_edit = _track_stop_before_edit(source_view, interaction, fail=True)

    asyncio.run(
        adapter.show_normal_scoring_targets(
            interaction,  # type: ignore[arg-type]
            context=context,
            source_view=source_view,
        )
    )

    assert stop_calls == [True]
    assert stopped_at_edit == [True]
    assert "`/win5 staff round`를 다시 열어" in interaction.response.messages[0][0]


def test_normal_scoring_cancel_failure_still_closes_source() -> None:
    adapter = _adapter(RecordingQueries())
    interaction = RecordingInteraction()
    view = Win5NormalScoringConfirmView(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(interaction),
        preview=Win5NormalScoringPreview(target=_target()),
    )
    stop_calls, stopped_at_edit = _track_stop_before_edit(view, interaction, fail=True)

    asyncio.run(view.children[1].callback(interaction))  # type: ignore[arg-type]

    assert stop_calls == [True]
    assert stopped_at_edit == [True]


def test_special_scoring_confirmation_uses_final_interaction_id_and_atomic_command() -> None:
    preview = Win5SpecialScoringPreview(target=_special_target())
    scoring_commands = RecordingSpecialScoringCommands(result=_scored_special_result())
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        special_scoring_commands=scoring_commands,
        preparation=preparation,
    )
    interaction = RecordingInteraction(interaction_id=999)
    view = Win5SpecialScoringConfirmView(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(interaction),
        preview=preview,
    )
    stop_calls = _track_stop(view)
    original_score_round = scoring_commands.score_round

    def score_round_after_view_closed(command: object) -> object:
        assert stop_calls == [True]
        return original_score_round(command)  # type: ignore[arg-type]

    scoring_commands.score_round = score_round_after_view_closed  # type: ignore[method-assign]
    event_loop_thread_id = get_ident()

    asyncio.run(view.children[0].callback(interaction))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "win5.staff.round", True)]
    command, worker_thread_id = scoring_commands.calls[0]
    assert worker_thread_id != event_loop_thread_id
    assert command.round_id == 21
    assert command.idempotency_key == "win5-special-scoring:999"
    assert command.actor_discord_user_id == "123"
    assert command.guild_id == "987"
    assert command.correlation_id == "999"
    assert command.reason is None
    assert interaction.edits[0]["content"] == (
        "WIN5 특별 라운드 채점 완료\n"
        "라운드: 여름 @\u200beveryone Special\n"
        "채점 Race: 3개\n"
        "채점 제출: 1건\n"
        "시즌 승점 합계: 1점\n"
        "TOP1 승점 합계: 1점\n"
        "서클 포인트 변경: 없음"
    )
    assert interaction.edits[0]["view"] is None
    assert stop_calls == [True]


def test_special_scoring_confirmation_is_claimed_once() -> None:
    scoring_commands = RecordingSpecialScoringCommands(result=_scored_special_result())
    adapter = _adapter(RecordingQueries(), special_scoring_commands=scoring_commands)
    opening = RecordingInteraction(interaction_id=998)
    view = Win5SpecialScoringConfirmView(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(opening),
        preview=Win5SpecialScoringPreview(target=_special_target()),
    )
    retry = RecordingInteraction(interaction_id=999)

    asyncio.run(view.children[0].callback(opening))  # type: ignore[arg-type]
    asyncio.run(view.children[0].callback(retry))  # type: ignore[arg-type]

    assert len(scoring_commands.calls) == 1
    assert "이미 처리 중이거나 완료" in retry.response.messages[0][0]


def test_special_scoring_transition_failure_closes_source_and_sends_reopen_notice() -> None:
    adapter = _adapter(RecordingQueries(special_choices=_special_choices(mode=Win5SpecialResultTargetMode.CORRECTION)))
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5StaffRoundActionView(adapter=adapter, context=context)
    stop_calls, stopped_at_edit = _track_stop_before_edit(source_view, interaction, fail=True)

    asyncio.run(
        adapter.show_special_scoring_targets(
            interaction,  # type: ignore[arg-type]
            context=context,
            source_view=source_view,
        )
    )

    assert stop_calls == [True]
    assert stopped_at_edit == [True]
    assert "`/win5 staff round`를 다시 열어" in interaction.response.messages[0][0]


def test_special_scoring_cancel_failure_still_closes_source() -> None:
    adapter = _adapter(RecordingQueries())
    interaction = RecordingInteraction()
    view = Win5SpecialScoringConfirmView(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(interaction),
        preview=Win5SpecialScoringPreview(target=_special_target()),
    )
    stop_calls, stopped_at_edit = _track_stop_before_edit(view, interaction, fail=True)

    asyncio.run(view.children[1].callback(interaction))  # type: ignore[arg-type]

    assert stop_calls == [True]
    assert stopped_at_edit == [True]


def test_stale_confirmation_closes_view_with_fixed_message_without_exception_detail() -> None:
    target = _target()
    placements = tuple(
        Win5NormalResultPlacementInput(position=position, race_entry_id=100 + position) for position in range(1, 6)
    )
    preview = SimpleNamespace(target=target, placements=placements, reason=None)
    commands = RecordingCommands(error=Win5NormalResultVersionConflictError("secret-current-fingerprint"))
    adapter = _adapter(RecordingQueries(), commands)
    interaction = RecordingInteraction()
    view = Win5NormalResultConfirmView(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(interaction),
        preview=preview,  # type: ignore[arg-type]
    )

    asyncio.run(view.children[0].callback(interaction))  # type: ignore[arg-type]

    content = interaction.edits[0]["content"]
    assert "미리보기 이후 결과가 변경되었습니다" in content
    assert "secret-current-fingerprint" not in content
    assert interaction.edits[0]["view"] is None


def test_stale_special_confirmation_closes_view_without_exception_detail() -> None:
    target = _special_target()
    winners = tuple(
        Win5SpecialResultWinnerInput(race_id=race_id, gate_number=gate_number)
        for race_id, gate_number in ((101, 1), (102, 9), (103, 3))
    )
    preview = Win5SpecialResultPreview(target=target, winners=winners, reason=None)
    special_commands = RecordingSpecialCommands(
        error=Win5SpecialResultVersionConflictError("secret-current-fingerprint")
    )
    adapter = _adapter(RecordingQueries(), special_commands=special_commands)
    interaction = RecordingInteraction()
    view = Win5SpecialResultConfirmView(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(interaction),
        preview=preview,
    )

    asyncio.run(view.children[0].callback(interaction))  # type: ignore[arg-type]

    content = interaction.edits[0]["content"]
    assert "미리보기 이후 결과가 변경되었습니다" in content
    assert "secret-current-fingerprint" not in content
    assert interaction.edits[0]["view"] is None


def test_scoring_wallet_failure_closes_view_without_leaking_exception_detail() -> None:
    preview = Win5NormalScoringPreview(target=_target())
    scoring_commands = RecordingScoringCommands(error=Win5NormalScoringWalletUnavailableError("secret-persona-id"))
    adapter = _adapter(RecordingQueries(), scoring_commands=scoring_commands)
    interaction = RecordingInteraction()
    view = Win5NormalScoringConfirmView(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(interaction),
        preview=preview,
    )

    asyncio.run(view.children[0].callback(interaction))  # type: ignore[arg-type]

    content = interaction.edits[0]["content"]
    assert "서클 포인트 지갑이 없어 전체 채점을 취소했습니다" in content
    assert "secret-persona-id" not in content
    assert interaction.edits[0]["view"] is None


def test_scoring_confirmation_context_mismatch_rejects_before_command() -> None:
    scoring_commands = RecordingScoringCommands(result=_scored_result())
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        scoring_commands=scoring_commands,
        preparation=preparation,
    )
    original = RecordingInteraction(user_id=123)
    other = RecordingInteraction(user_id=456)
    view = Win5NormalScoringConfirmView(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(original),
        preview=Win5NormalScoringPreview(target=_target()),
    )

    asyncio.run(view.children[0].callback(other))  # type: ignore[arg-type]

    assert preparation.calls == []
    assert scoring_commands.calls == []
    assert other.response.messages[0][0] == "이 확인 화면을 연 사용자와 서버·채널에서만 채점을 확정할 수 있습니다."


def test_special_scoring_confirmation_context_mismatch_rejects_before_command() -> None:
    scoring_commands = RecordingSpecialScoringCommands(result=_scored_special_result())
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        special_scoring_commands=scoring_commands,
        preparation=preparation,
    )
    original = RecordingInteraction(user_id=123)
    other = RecordingInteraction(user_id=456)
    view = Win5SpecialScoringConfirmView(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(original),
        preview=Win5SpecialScoringPreview(target=_special_target()),
    )

    asyncio.run(view.children[0].callback(other))  # type: ignore[arg-type]

    assert preparation.calls == []
    assert scoring_commands.calls == []
    assert other.response.messages[0][0] == (
        "이 확인 화면을 연 사용자와 서버·채널에서만 특별 채점을 확정할 수 있습니다."
    )


def test_context_mismatch_rejects_before_authorization_query_or_command() -> None:
    queries = RecordingQueries(choices=_choices())
    commands = RecordingCommands()
    authorization = RecordingAuthorization()
    adapter = _adapter(queries, commands, authorization=authorization)
    original = RecordingInteraction(user_id=123)
    other = RecordingInteraction(user_id=456)

    asyncio.run(
        adapter.show_normal_result_targets(
            other,  # type: ignore[arg-type]
            context=Win5StaffInteractionContext.from_interaction(original),
            mode=Win5NormalResultTargetMode.ENTRY,
        )
    )

    assert authorization.calls == []
    assert queries.list_calls == []
    assert commands.calls == []
    assert other.response.messages[0][0] == "이 화면을 연 사용자와 서버·채널에서만 계속할 수 있습니다."


def test_special_void_flow_queries_fresh_state_collects_reason_and_confirms_once() -> None:
    target = _special_void_target(result_count=3)
    void_queries = RecordingSpecialVoidQueries(
        choices=_special_void_choices(),
        target=target,
    )
    void_commands = RecordingSpecialVoidCommands(result=_updated_special_void())
    authorization = RecordingAuthorization()
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        special_void_queries=void_queries,
        special_void_commands=void_commands,
        authorization=authorization,
        preparation=preparation,
    )
    opening = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(opening)
    event_loop_thread_id = get_ident()
    action_view = Win5StaffRoundActionView(adapter=adapter, context=context)
    action = action_view.children[0]
    action._values = ["special-void"]
    action_stop_calls, action_stopped_at_edit = _track_stop_before_edit(action_view, opening)
    asyncio.run(action.callback(opening))  # type: ignore[arg-type]

    assert action_stop_calls == [True]
    assert action_stopped_at_edit == [True]
    target_view = opening.response.edits[0]["view"]
    assert isinstance(target_view, Win5SpecialVoidTargetView)
    target_select = target_view.children[0]
    assert [option.value for option in target_select.options] == ["21", "22"]
    assert [option.description for option in target_select.options] == [
        "Race 3개 · 취소 0개",
        "Race 4개 · 취소 1개",
    ]
    assert void_queries.list_calls[0][1] != event_loop_thread_id

    race_interaction = RecordingInteraction()
    target_stop_calls, target_stopped_at_edit = _track_stop_before_edit(target_view, race_interaction)
    target_select._values = ["21"]
    asyncio.run(target_select.callback(race_interaction))  # type: ignore[arg-type]
    assert target_stop_calls == [True]
    assert target_stopped_at_edit == [True]
    race_view = race_interaction.response.edits[0]["view"]
    assert isinstance(race_view, Win5SpecialVoidRaceView)
    race_select = race_view.children[0]
    assert [option.label for option in race_select.options] == [
        "정상 · Race 1 @\u200beveryone",
        "정상 · Race 2 @\u200beveryone",
        "정상 · Race 3 @\u200beveryone",
    ]

    modal_interaction = RecordingInteraction()
    race_stop_calls = _track_stop(race_view)
    race_select._values = ["101"]
    asyncio.run(race_select.callback(modal_interaction))  # type: ignore[arg-type]
    modal = modal_interaction.response.modals[0]
    assert isinstance(modal, Win5SpecialVoidReasonModal)
    assert modal.reason.required is True
    assert modal._source_view is race_view
    assert race_stop_calls == []
    modal.reason._value = "  공식 취소 확인  "

    preview_interaction = RecordingInteraction(interaction_id=600)
    race_stop_calls, preview_stopped_at_edit = _track_stop_before_edit(
        race_view,
        preview_interaction,
        deferred=True,
    )
    asyncio.run(modal.on_submit(preview_interaction))  # type: ignore[arg-type]
    assert race_stop_calls == [True]
    assert preview_stopped_at_edit == [True]
    preview_edit = preview_interaction.edits[0]
    assert "현재 미채점 특별 결과 3건은 전체 삭제" in preview_edit["content"]
    confirm_view = preview_edit["view"]
    assert isinstance(confirm_view, Win5SpecialVoidConfirmView)

    confirm_interaction = RecordingInteraction(interaction_id=777)
    confirm_stop_calls = _track_stop(confirm_view)
    original_set_race_void = void_commands.set_race_void

    def set_race_void_after_view_closed(command: object) -> object:
        assert confirm_stop_calls == [True]
        return original_set_race_void(command)  # type: ignore[arg-type]

    void_commands.set_race_void = set_race_void_after_view_closed  # type: ignore[method-assign]
    asyncio.run(confirm_view.children[0].callback(confirm_interaction))  # type: ignore[arg-type]

    assert len(void_commands.calls) == 1
    command, worker_thread_id = void_commands.calls[0]
    assert command == SetSpecialWin5RaceVoid(
        round_id=21,
        race_id=101,
        voided=True,
        expected_void_fingerprint=target.void_fingerprint,
        idempotency_key="win5-special-void:777",
        actor_discord_user_id="123",
        reason="공식 취소 확인",
        guild_id="987",
        correlation_id="777",
    )
    assert worker_thread_id != event_loop_thread_id
    assert "취소 완료" in confirm_interaction.edits[0]["content"]

    duplicate = RecordingInteraction(interaction_id=778)
    asyncio.run(confirm_view.children[0].callback(duplicate))  # type: ignore[arg-type]
    assert len(void_commands.calls) == 1
    assert "이미 처리 중이거나 완료" in duplicate.response.messages[0][0]


def test_special_void_race_pages_keep_full_target_and_requery_navigation() -> None:
    races = tuple(Win5StaffSpecialVoidRace(id=100 + index, name=f"Race {index}") for index in range(1, 27))
    target = Win5StaffSpecialVoidTarget(
        season_id=7,
        season_name="Season",
        round_id=21,
        round_name="Long Special",
        races=races,
        result_count=0,
        void_fingerprint=fingerprint_special_void_state(()),
    )
    queries = RecordingSpecialVoidQueries(target=target)
    adapter = _adapter(RecordingQueries(), special_void_queries=queries)
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)

    asyncio.run(
        adapter.show_special_void_races(
            interaction,  # type: ignore[arg-type]
            context=context,
            round_id=21,
            page=0,
        )
    )
    first_view = interaction.response.edits[0]["view"]
    assert isinstance(first_view, Win5SpecialVoidRaceView)
    assert len(first_view.children[0].options) == 25

    next_interaction = RecordingInteraction()
    stop_calls, stopped_at_edit = _track_stop_before_edit(first_view, next_interaction)
    asyncio.run(first_view.children[1].callback(next_interaction))  # type: ignore[arg-type]
    assert stop_calls == [True]
    assert stopped_at_edit == [True]
    second_view = next_interaction.response.edits[0]["view"]
    assert isinstance(second_view, Win5SpecialVoidRaceView)
    assert len(second_view.children[0].options) == 1
    assert second_view.children[0].options[0].value == "126"
    assert [round_id for round_id, _ in queries.get_calls] == [21, 21]


def test_special_void_preview_emphasizes_all_void_terminal_effect_and_cancel_is_zero_write() -> None:
    races = tuple(
        Win5StaffSpecialVoidRace(
            id=race_id,
            name=f"Race {index} @everyone",
            void_reason="공식 취소" if index < 3 else None,
            voided_at=datetime(2026, 8, 26, 1, 0, tzinfo=UTC) if index < 3 else None,
        )
        for index, race_id in enumerate((101, 102, 103), start=1)
    )
    target = Win5StaffSpecialVoidTarget(
        season_id=7,
        season_name="2026 @everyone",
        round_id=21,
        round_name="Special @everyone",
        races=races,
        result_count=0,
        void_fingerprint=fingerprint_special_void_state((101, 102)),
    )
    preview = Win5SpecialVoidPreview(
        target=target,
        race_id=103,
        target_voided=True,
        reason="전 경기 취소 @everyone",
    )

    message = format_special_void_preview(preview)

    assert "Round 전체가 cancelled" in message
    assert "채점·승점·서클 포인트·결과 publication은 생성하지 않으며" in message
    assert "@everyone" not in message

    commands = RecordingSpecialVoidCommands(result=_updated_special_void())
    adapter = _adapter(RecordingQueries(), special_void_commands=commands)
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    view = Win5SpecialVoidConfirmView(adapter=adapter, context=context, preview=preview)
    stop_calls, stopped_at_edit = _track_stop_before_edit(view, interaction, fail=True)

    asyncio.run(view.children[1].callback(interaction))  # type: ignore[arg-type]

    assert commands.calls == []
    assert stop_calls == [True]
    assert stopped_at_edit == [True]


def test_special_void_transition_failure_closes_source_and_sends_reopen_notice() -> None:
    adapter = _adapter(
        RecordingQueries(),
        special_void_queries=RecordingSpecialVoidQueries(choices=_special_void_choices()),
    )
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5StaffRoundActionView(adapter=adapter, context=context)
    stop_calls, stopped_at_edit = _track_stop_before_edit(source_view, interaction, fail=True)

    asyncio.run(
        adapter.show_special_void_targets(
            interaction,  # type: ignore[arg-type]
            context=context,
            source_view=source_view,
        )
    )

    assert stop_calls == [True]
    assert stopped_at_edit == [True]
    assert "`/win5 staff round`를 다시 열어" in interaction.response.messages[0][0]


def test_special_round_cancellation_is_one_atomic_all_void_command() -> None:
    target = _special_void_target(first_void=True, result_count=2)
    queries = RecordingSpecialVoidQueries(
        choices=_special_void_choices(),
        target=target,
    )
    commands = RecordingSpecialVoidCommands(cancellation_result=_cancelled_special_round())
    adapter = _adapter(
        RecordingQueries(),
        special_void_queries=queries,
        special_void_commands=commands,
    )
    opening = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(opening)
    event_loop_thread_id = get_ident()
    action_view = Win5StaffRoundActionView(adapter=adapter, context=context)
    action = action_view.children[0]
    action._values = ["special-round-cancel"]
    action_stop_calls, action_stopped_at_edit = _track_stop_before_edit(action_view, opening)
    asyncio.run(action.callback(opening))  # type: ignore[arg-type]

    assert action_stop_calls == [True]
    assert action_stopped_at_edit == [True]
    target_view = opening.response.edits[0]["view"]
    assert isinstance(target_view, Win5SpecialRoundCancellationTargetView)
    target_select = target_view.children[0]
    target_interaction = RecordingInteraction()
    target_stop_calls = _track_stop(target_view)
    target_select._values = ["21"]
    asyncio.run(target_select.callback(target_interaction))  # type: ignore[arg-type]

    modal = target_interaction.response.modals[0]
    assert isinstance(modal, Win5SpecialRoundCancellationReasonModal)
    assert modal.reason.required is True
    assert modal._source_view is target_view
    assert target_stop_calls == []
    modal.reason._value = "  태풍으로 전체 행사 취소 @everyone  "

    preview_interaction = RecordingInteraction(interaction_id=800)
    target_stop_calls, preview_stopped_at_edit = _track_stop_before_edit(
        target_view,
        preview_interaction,
        deferred=True,
    )
    asyncio.run(modal.on_submit(preview_interaction))  # type: ignore[arg-type]
    assert target_stop_calls == [True]
    assert preview_stopped_at_edit == [True]

    preview_edit = preview_interaction.edits[0]
    assert "이번 취소 2개" in preview_edit["content"]
    assert "현재 미채점 특별 결과 2건은 전체 삭제" in preview_edit["content"]
    assert "Round를 cancelled로 종료" in preview_edit["content"]
    assert "@everyone" not in preview_edit["content"]
    confirm_view = preview_edit["view"]
    assert isinstance(confirm_view, Win5SpecialRoundCancellationConfirmView)

    confirm_interaction = RecordingInteraction(interaction_id=888)
    confirm_stop_calls = _track_stop(confirm_view)
    original_cancel_round = commands.cancel_round

    def cancel_round_after_view_closed(command: object) -> object:
        assert confirm_stop_calls == [True]
        return original_cancel_round(command)  # type: ignore[arg-type]

    commands.cancel_round = cancel_round_after_view_closed  # type: ignore[method-assign]
    asyncio.run(confirm_view.children[0].callback(confirm_interaction))  # type: ignore[arg-type]

    assert commands.calls == []
    assert len(commands.cancellation_calls) == 1
    command, worker_thread_id = commands.cancellation_calls[0]
    assert command == CancelSpecialWin5Round(
        round_id=21,
        expected_void_fingerprint=target.void_fingerprint,
        idempotency_key="win5-special-round-cancel:888",
        actor_discord_user_id="123",
        reason="태풍으로 전체 행사 취소 @everyone",
        guild_id="987",
        correlation_id="888",
    )
    assert worker_thread_id != event_loop_thread_id
    assert "전체 취소 완료" in confirm_interaction.edits[0]["content"]

    duplicate = RecordingInteraction(interaction_id=889)
    asyncio.run(confirm_view.children[0].callback(duplicate))  # type: ignore[arg-type]
    assert len(commands.cancellation_calls) == 1
    assert "이미 처리 중이거나 완료" in duplicate.response.messages[0][0]


def test_special_round_cancellation_preview_cancel_is_zero_write() -> None:
    preview = Win5SpecialRoundCancellationPreview(
        target=_special_void_target(first_void=True),
        reason="전체 행사 취소",
    )
    assert "이번 취소 2개" in format_special_round_cancellation_preview(preview)
    commands = RecordingSpecialVoidCommands(cancellation_result=_cancelled_special_round())
    adapter = _adapter(RecordingQueries(), special_void_commands=commands)
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    view = Win5SpecialRoundCancellationConfirmView(adapter=adapter, context=context, preview=preview)
    stop_calls, stopped_at_edit = _track_stop_before_edit(view, interaction, fail=True)

    asyncio.run(view.children[1].callback(interaction))  # type: ignore[arg-type]

    assert commands.cancellation_calls == []
    assert stop_calls == [True]
    assert stopped_at_edit == [True]


def test_special_round_cancellation_transition_failure_closes_source_and_sends_reopen_notice() -> None:
    adapter = _adapter(
        RecordingQueries(),
        special_void_queries=RecordingSpecialVoidQueries(choices=_special_void_choices()),
    )
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5StaffRoundActionView(adapter=adapter, context=context)
    stop_calls, stopped_at_edit = _track_stop_before_edit(source_view, interaction, fail=True)

    asyncio.run(
        adapter.show_special_round_cancellation_targets(
            interaction,  # type: ignore[arg-type]
            context=context,
            source_view=source_view,
        )
    )

    assert stop_calls == [True]
    assert stopped_at_edit == [True]
    assert "`/win5 staff round`를 다시 열어" in interaction.response.messages[0][0]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1-3-5-2-4", (1, 3, 5, 2, 4)),
        (" 1, 3,5, 2,4 ", (1, 3, 5, 2, 4)),
    ],
)
def test_normal_result_gate_parser_accepts_bounded_legacy_delimiters(
    value: str,
    expected: tuple[int, ...],
) -> None:
    assert parse_normal_result_gate_numbers(value) == expected


@pytest.mark.parametrize("value", ["1-2-3-4", "1-2-3-4-4", "-1-2-3-4-5", "1 2 3 4 5"])
def test_normal_result_gate_parser_rejects_incomplete_duplicate_or_ambiguous_input(value: str) -> None:
    with pytest.raises(ValueError):
        parse_normal_result_gate_numbers(value)


def test_special_result_gate_parser_requires_one_positive_integer_per_race() -> None:
    assert parse_special_result_gate_numbers(" 1\n\n9\n3 ", expected_count=3) == (1, 9, 3)
    assert parse_special_result_gate_numbers("1\n1\n1", expected_count=3) == (1, 1, 1)
    with pytest.raises(ValueError, match="Race 3개"):
        parse_special_result_gate_numbers("1\n2", expected_count=3)
    with pytest.raises(ValueError, match="Race 3개"):
        parse_special_result_gate_numbers("1\nvoid\n3", expected_count=3)


def test_special_result_preview_refuses_to_hide_part_of_complete_bundle() -> None:
    races = tuple(
        Win5SpecialResultRace(
            id=100 + index,
            name=f"{'긴이름' * 60}-{index}",
        )
        for index in range(1, 13)
    )
    target = Win5SpecialResultTarget(
        season_id=7,
        season_name="시즌",
        round_id=21,
        round_name="Special",
        mode=Win5SpecialResultTargetMode.ENTRY,
        races=races,
    )
    preview = Win5SpecialResultPreview(
        target=target,
        winners=tuple(
            Win5SpecialResultWinnerInput(race_id=race.id, gate_number=index)
            for index, race in enumerate(races, start=1)
        ),
        reason=None,
    )

    with pytest.raises(ValueError, match="전체 Special 결과"):
        format_special_result_preview(preview)


def test_preview_formatter_is_mention_safe_and_bounded() -> None:
    target = _target(mode=Win5NormalResultTargetMode.ENTRY)
    placements = tuple(
        Win5NormalResultPlacementInput(position=position, race_entry_id=100 + position) for position in range(1, 6)
    )
    message = format_normal_result_preview(
        SimpleNamespace(target=target, placements=placements, reason="@everyone " + ("x" * 255))  # type: ignore[arg-type]
    )

    assert len(message) <= 1900
    assert "@everyone" not in message

    scoring_message = format_normal_scoring_preview(Win5NormalScoringPreview(target=_target()))
    assert len(scoring_message) <= 1900
    assert "@everyone" not in scoring_message

    special_scoring_message = format_special_scoring_preview(Win5SpecialScoringPreview(target=_special_target()))
    assert len(special_scoring_message) <= 1900
    assert "@everyone" not in special_scoring_message

    mixed_void_message = format_special_scoring_preview(
        Win5SpecialScoringPreview(target=_special_target(void_ids=(102,)))
    )
    assert "Race 2 @\u200beveryone: VOID — 공식 취소 @\u200beveryone" in mixed_void_message
    assert "Race 1 @\u200beveryone: 3" in mixed_void_message
    assert "Race 3 @\u200beveryone: 1" in mixed_void_message
