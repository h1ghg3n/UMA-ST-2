"""Discord adapter tests for WIN5 member submission workflows."""

from __future__ import annotations

import pytest
from win5_member_discord_test_support import (
    CancelledWin5Submission,
    RecordingAutocompleteAuthorization,
    RecordingCommands,
    RecordingInteraction,
    RecordingInteractionAuthorization,
    RecordingPreparation,
    RecordingQueries,
    SavedWin5Submission,
    Win5AcceptedNormalSubmission,
    Win5CancellableSubmissionUnavailableError,
    Win5MemberApprovalPendingError,
    Win5MemberCommandGroup,
    Win5MemberInteractionContext,
    Win5MemberQueryApprovalPendingError,
    Win5NormalSubmissionCancelConfirmView,
    Win5NormalSubmissionDraft,
    Win5NormalSubmissionEditorView,
    Win5NormalSubmissionPick,
    Win5NormalSubmissionRoundChoice,
    Win5SpecialSubmissionCancelConfirmView,
    Win5SpecialSubmissionDraft,
    Win5SpecialSubmissionEditorView,
    Win5SpecialSubmissionModal,
    Win5SpecialSubmissionPick,
    Win5SpecialSubmissionRoundChoice,
    Win5SubmissionCancelConfirmView,
    Win5SubmissionPickAuditSnapshot,
    Win5SubmissionStatus,
    Win5SubmissionTier,
    Win5SubmissionVersionConflictError,
    _accepted_special_submission,
    _accepted_submission,
    _adapter,
    _cancellable_submission,
    _layout_items,
    _layout_text,
    _normal_editor,
    _special_editor,
    app_commands,
    asyncio,
    discord,
    get_ident,
)


def test_submit_registration_opens_private_layout_editor_through_worker() -> None:
    editor = _normal_editor()
    queries = RecordingQueries(normal_editor=editor)
    preparation = RecordingPreparation()
    group = Win5MemberCommandGroup(adapter=_adapter(queries, preparation))
    command = group.get_command("submit")
    interaction = RecordingInteraction()
    event_loop_thread_id = get_ident()

    assert isinstance(command, app_commands.Command)
    assert command.description == "열린 일반 라운드의 WIN5 예측을 편집합니다."
    assert len(command.parameters) == 1
    assert command.parameters[0].name == "round_id"
    assert command.parameters[0].required is True
    assert command.parameters[0].autocomplete is True

    asyncio.run(command.callback(group, interaction, 11))  # type: ignore[arg-type,union-attr]

    assert preparation.calls == [(interaction, "win5.submit", True)]
    assert queries.normal_editor_calls[0][:2] == ("123", 11)
    assert queries.normal_editor_calls[0][2] != event_loop_thread_id
    assert len(interaction.edits) == 1
    assert interaction.edits[0]["content"] is None
    view = interaction.edits[0]["view"]
    assert isinstance(view, Win5NormalSubmissionEditorView)
    text = _layout_text(view)
    assert "Tier를 먼저 선택" in text
    assert "@everyone" not in text
    assert view.total_children_count <= 40


def test_pending_approval_open_editor_uses_distinct_private_guidance() -> None:
    queries = RecordingQueries(normal_editor_error=Win5MemberQueryApprovalPendingError("internal pending identity"))
    preparation = RecordingPreparation()
    group = Win5MemberCommandGroup(adapter=_adapter(queries, preparation))
    command = group.get_command("submit")
    interaction = RecordingInteraction()

    asyncio.run(command.callback(group, interaction, 11))  # type: ignore[arg-type,union-attr]

    assert preparation.calls == [(interaction, "win5.submit", True)]
    content = interaction.edits[0]["content"]
    assert "계정 승인을 기다리는 중" in content
    assert "internal pending identity" not in content


def test_submit_round_autocomplete_authorizes_bounds_and_disambiguates_titles() -> None:
    choices = (
        Win5NormalSubmissionRoundChoice(7, "시즌", 11, "Round", 1101, "Race"),
        Win5NormalSubmissionRoundChoice(7, "시즌", 12, "Round", 1201, "Race"),
    )
    queries = RecordingQueries(normal_round_choices=choices)
    authorization = RecordingAutocompleteAuthorization()
    group = Win5MemberCommandGroup(adapter=_adapter(queries, RecordingPreparation(), authorization))
    interaction = RecordingInteraction()
    event_loop_thread_id = get_ident()

    results = asyncio.run(
        group._normal_submission_round_autocomplete(interaction, "Round")  # type: ignore[arg-type]
    )

    assert authorization.calls == [(interaction, "win5.submit")]
    assert queries.normal_round_search_calls[0][:3] == ("123", "Round", 25)
    assert queries.normal_round_search_calls[0][3] != event_loop_thread_id
    assert [(choice.name, choice.value) for choice in results] == [
        ("Round · Race · ID 11", 11),
        ("Round · Race · ID 12", 12),
    ]


def test_prefilled_top5_layout_pages_entries_without_discord_cardinality_leak() -> None:
    submission = Win5AcceptedNormalSubmission(
        id=501,
        tier=Win5SubmissionTier.TOP5,
        version=4,
        picks=tuple(
            Win5NormalSubmissionPick(position=position, race_entry_id=entry_id)
            for position, entry_id in enumerate(
                (2001, 2024, 2025, 2046, 2047),
                start=1,
            )
        ),
    )
    editor = _normal_editor(entry_count=47, submission=submission)
    adapter = _adapter(RecordingQueries(), RecordingPreparation())
    context = Win5MemberInteractionContext.from_interaction(RecordingInteraction())

    view = Win5NormalSubmissionEditorView(
        adapter=adapter,
        context=context,
        editor=editor,
        draft=Win5NormalSubmissionDraft.from_editor(editor),
    )

    selects = _layout_items(view, discord.ui.Select)
    assert len(selects) == 6
    pick_selects = [
        select
        for select in selects
        if isinstance(select, discord.ui.Select) and select.custom_id.startswith("win5-normal-submission-pick-")
    ]
    assert len(pick_selects) == 5
    assert all(len(select.options) <= 25 for select in pick_selects)
    assert all(sum(option.default for option in select.options) == 1 for select in pick_selects)
    assert "엔트리 페이지: 1 / 3" in _layout_text(view)
    assert view.total_children_count <= 40
    assert view.content_length() <= 4000
    assert len(view.to_components()) == 1
    custom_ids = [item.custom_id for item in view.walk_children() if item.is_dispatchable()]
    assert len(custom_ids) == len(set(custom_ids))


def test_all_clear_draft_disables_save_but_keeps_explicit_cancellation() -> None:
    editor = _normal_editor(submission=_accepted_submission())
    view = Win5NormalSubmissionEditorView(
        adapter=_adapter(RecordingQueries(), RecordingPreparation()),
        context=Win5MemberInteractionContext.from_interaction(RecordingInteraction()),
        editor=editor,
        draft=Win5NormalSubmissionDraft(tier=Win5SubmissionTier.TOP3),
    )

    buttons = [button for button in _layout_items(view, discord.ui.Button) if isinstance(button, discord.ui.Button)]
    save = next(button for button in buttons if button.label == "변경 저장")
    cancel = next(button for button in buttons if button.label == "제출 취소")
    assert save.disabled is True
    assert cancel.disabled is False
    assert "빈 상태를 저장하지 말고 `제출 취소`" in _layout_text(view)


def test_zero_pick_save_callback_stops_before_authorization_or_application() -> None:
    editor = _normal_editor(submission=_accepted_submission())
    draft = Win5NormalSubmissionDraft(tier=Win5SubmissionTier.TOP3)
    commands = RecordingCommands()
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        preparation,
        commands=commands,
    )
    interaction = RecordingInteraction()
    context = Win5MemberInteractionContext.from_interaction(interaction)
    source_view = Win5NormalSubmissionEditorView(
        adapter=adapter,
        context=context,
        editor=editor,
        draft=draft,
    )

    asyncio.run(
        adapter.save_normal_submission(
            interaction,  # type: ignore[arg-type]
            context=context,
            editor=editor,
            draft=draft,
            source_view=source_view,
        )
    )

    assert preparation.calls == []
    assert commands.save_calls == []
    assert interaction.response.messages
    assert "하나 이상의 pick" in interaction.response.messages[0][0]


def test_versioned_save_translates_draft_and_returns_prefilled_new_version() -> None:
    current = _accepted_submission()
    editor = _normal_editor(submission=current)
    draft = Win5NormalSubmissionDraft(
        tier=Win5SubmissionTier.TOP3,
        picks=(
            Win5NormalSubmissionPick(position=1, race_entry_id=2002),
            Win5NormalSubmissionPick(position=2, race_entry_id=2001),
        ),
    )
    result = SavedWin5Submission(
        season_id=7,
        round_id=11,
        submission_id=501,
        persona_id="persona-1",
        tier=Win5SubmissionTier.TOP3,
        status=Win5SubmissionStatus.ACCEPTED,
        version=5,
        picks=(
            Win5SubmissionPickAuditSnapshot(1101, 1, 2002, None),
            Win5SubmissionPickAuditSnapshot(1101, 2, 2001, None),
        ),
    )
    commands = RecordingCommands(save_result=result)
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        preparation,
        commands=commands,
    )
    interaction = RecordingInteraction(interaction_id=777)
    context = Win5MemberInteractionContext.from_interaction(interaction)
    source_view = Win5NormalSubmissionEditorView(
        adapter=adapter,
        context=context,
        editor=editor,
        draft=draft,
    )
    original_stop = source_view.stop
    stop_calls: list[bool] = []

    def record_stop() -> None:
        stop_calls.append(True)
        original_stop()

    source_view.stop = record_stop  # type: ignore[method-assign]
    original_save = commands.save_submission
    source_finished_at_command: list[bool] = []

    def record_save(command: object) -> SavedWin5Submission:
        source_finished_at_command.append(bool(stop_calls))
        return original_save(command)

    commands.save_submission = record_save  # type: ignore[method-assign]
    event_loop_thread_id = get_ident()

    asyncio.run(
        adapter.save_normal_submission(
            interaction,  # type: ignore[arg-type]
            context=context,
            editor=editor,
            draft=draft,
            source_view=source_view,
        )
    )

    assert preparation.calls == [(interaction, "win5.submit", True)]
    assert source_finished_at_command == [True]
    command, worker_thread_id = commands.save_calls[0]
    assert worker_thread_id != event_loop_thread_id
    assert command.round_id == 11
    assert command.submission_id == 501
    assert command.expected_version == 4
    assert command.actor_discord_user_id == "123"
    assert command.guild_id == "987"
    assert command.correlation_id == "777"
    assert [(pick.position, pick.race_entry_id) for pick in command.picks] == [
        (1, 2002),
        (2, 2001),
    ]
    next_view = interaction.edits[0]["view"]
    assert isinstance(next_view, Win5NormalSubmissionEditorView)
    assert next_view.editor.submission is not None
    assert next_view.editor.submission.version == 5
    assert "접수 성공 · version 5" in _layout_text(next_view)
    assert "1착: 2 · Horse 2" in _layout_text(next_view)
    assert "3착: —" in _layout_text(next_view)

    duplicate = RecordingInteraction(interaction_id=778)
    asyncio.run(
        adapter.save_normal_submission(
            duplicate,  # type: ignore[arg-type]
            context=context,
            editor=editor,
            draft=draft,
            source_view=source_view,
        )
    )

    assert len(commands.save_calls) == 1
    assert "이미 시작" in duplicate.response.messages[0][0]


def test_stale_save_is_fixed_terminal_message_without_internal_detail() -> None:
    editor = _normal_editor(submission=_accepted_submission())
    draft = Win5NormalSubmissionDraft.from_editor(editor)
    commands = RecordingCommands(error=Win5SubmissionVersionConflictError("current version=99 secret"))
    adapter = _adapter(
        RecordingQueries(),
        RecordingPreparation(),
        commands=commands,
    )
    interaction = RecordingInteraction()
    context = Win5MemberInteractionContext.from_interaction(interaction)
    source_view = Win5NormalSubmissionEditorView(
        adapter=adapter,
        context=context,
        editor=editor,
        draft=draft,
    )

    asyncio.run(
        adapter.save_normal_submission(
            interaction,  # type: ignore[arg-type]
            context=context,
            editor=editor,
            draft=draft,
            source_view=source_view,
        )
    )

    message = _layout_text(interaction.edits[0]["view"])
    assert "Submission이 변경" in message
    assert "current version=99" not in message


def test_pending_approval_save_uses_distinct_private_guidance() -> None:
    editor = _normal_editor(submission=_accepted_submission())
    draft = Win5NormalSubmissionDraft.from_editor(editor)
    commands = RecordingCommands(error=Win5MemberApprovalPendingError("internal pending identity"))
    adapter = _adapter(
        RecordingQueries(),
        RecordingPreparation(),
        commands=commands,
    )
    interaction = RecordingInteraction()
    context = Win5MemberInteractionContext.from_interaction(interaction)
    source_view = Win5NormalSubmissionEditorView(
        adapter=adapter,
        context=context,
        editor=editor,
        draft=draft,
    )

    asyncio.run(
        adapter.save_normal_submission(
            interaction,  # type: ignore[arg-type]
            context=context,
            editor=editor,
            draft=draft,
            source_view=source_view,
        )
    )

    message = _layout_text(interaction.edits[0]["view"])
    assert "계정 승인을 기다리는 중" in message
    assert "internal pending identity" not in message


def test_explicit_cancel_confirmation_executes_versioned_history_transition() -> None:
    editor = _normal_editor(submission=_accepted_submission())
    result = CancelledWin5Submission(
        season_id=7,
        round_id=11,
        submission_id=501,
        persona_id="persona-1",
        tier=Win5SubmissionTier.TOP3,
        status=Win5SubmissionStatus.CANCELLED,
        version=5,
    )
    commands = RecordingCommands(cancel_result=result)
    authorization = RecordingInteractionAuthorization()
    adapter = _adapter(
        RecordingQueries(),
        RecordingPreparation(),
        commands=commands,
        interaction_authorization=authorization,
    )
    interaction = RecordingInteraction(interaction_id=888)
    context = Win5MemberInteractionContext.from_interaction(interaction)
    draft = Win5NormalSubmissionDraft.from_editor(editor)
    editor_view = Win5NormalSubmissionEditorView(
        adapter=adapter,
        context=context,
        editor=editor,
        draft=draft,
    )

    asyncio.run(
        adapter.show_normal_submission_cancel_confirmation(
            interaction,  # type: ignore[arg-type]
            context=context,
            editor=editor,
            draft=draft,
            source_view=editor_view,
        )
    )

    confirm_view = interaction.response.edits[0]["view"]
    assert isinstance(confirm_view, Win5NormalSubmissionCancelConfirmView)
    assert authorization.calls == [(interaction, "win5.submit")]
    original_stop = confirm_view.stop
    stop_calls: list[bool] = []

    def record_stop() -> None:
        stop_calls.append(True)
        original_stop()

    confirm_view.stop = record_stop  # type: ignore[method-assign]
    original_cancel = commands.cancel_submission
    source_finished_at_command: list[bool] = []

    def record_cancel(command: object) -> CancelledWin5Submission:
        source_finished_at_command.append(bool(stop_calls))
        return original_cancel(command)

    commands.cancel_submission = record_cancel  # type: ignore[method-assign]

    asyncio.run(
        adapter.cancel_normal_submission(
            interaction,  # type: ignore[arg-type]
            context=context,
            editor=editor,
            source_view=confirm_view,
        )
    )

    command, _ = commands.cancel_calls[0]
    assert command.submission_id == 501
    assert command.expected_version == 4
    assert command.correlation_id == "888"
    assert source_finished_at_command == [True]
    assert "제출 취소 완료" in _layout_text(interaction.edits[0]["view"])

    duplicate = RecordingInteraction(interaction_id=889)
    asyncio.run(
        adapter.cancel_normal_submission(
            duplicate,  # type: ignore[arg-type]
            context=context,
            editor=editor,
            source_view=confirm_view,
        )
    )

    assert len(commands.cancel_calls) == 1
    assert "이미 시작" in duplicate.response.messages[0][0]


def test_component_context_mismatch_stops_before_authorization_or_mutation() -> None:
    editor = _normal_editor(submission=_accepted_submission())
    commands = RecordingCommands()
    authorization = RecordingInteractionAuthorization()
    adapter = _adapter(
        RecordingQueries(),
        RecordingPreparation(),
        commands=commands,
        interaction_authorization=authorization,
    )
    opener = RecordingInteraction()
    context = Win5MemberInteractionContext.from_interaction(opener)
    source_view = Win5NormalSubmissionEditorView(
        adapter=adapter,
        context=context,
        editor=editor,
        draft=Win5NormalSubmissionDraft.from_editor(editor),
    )
    intruder = RecordingInteraction(user_id=999)

    asyncio.run(
        adapter.change_normal_submission_page(
            intruder,  # type: ignore[arg-type]
            context=context,
            editor=editor,
            draft=source_view.draft,
            page=0,
            source_view=source_view,
        )
    )

    assert authorization.calls == []
    assert commands.save_calls == []
    assert commands.cancel_calls == []
    assert intruder.response.messages[0][0] == "이 화면을 연 사용자와 서버·채널에서만 계속할 수 있습니다."
    assert source_view.is_finished() is False


@pytest.mark.parametrize("edit_fails", (False, True), ids=("success", "delivery-failure"))
def test_same_message_replacement_stops_source_before_edit_and_recovers_failure(
    edit_fails: bool,
) -> None:
    editor = _normal_editor(submission=_accepted_submission())
    adapter = _adapter(RecordingQueries(), RecordingPreparation())
    context = Win5MemberInteractionContext.from_interaction(RecordingInteraction())
    draft = Win5NormalSubmissionDraft.from_editor(editor)
    source_view = Win5NormalSubmissionEditorView(
        adapter=adapter,
        context=context,
        editor=editor,
        draft=draft,
    )
    replacement_view = Win5NormalSubmissionEditorView(
        adapter=adapter,
        context=context,
        editor=editor,
        draft=draft,
    )
    interaction = RecordingInteraction()
    original_stop = source_view.stop
    stop_calls: list[bool] = []

    def record_stop() -> None:
        stop_calls.append(True)
        original_stop()

    source_view.stop = record_stop  # type: ignore[method-assign]
    original_edit = interaction.response.edit_message
    source_stopped_at_edit: list[bool] = []

    async def observe_edit(**kwargs: object) -> None:
        source_stopped_at_edit.append(bool(stop_calls))
        if edit_fails:
            raise RuntimeError("Discord component edit failed")
        await original_edit(**kwargs)

    interaction.response.edit_message = observe_edit  # type: ignore[method-assign]

    asyncio.run(
        adapter._replace_component_layout(  # noqa: SLF001
            interaction,  # type: ignore[arg-type]
            view=replacement_view,
            source_view=source_view,
        )
    )

    assert source_stopped_at_edit == [True]
    assert stop_calls == [True]
    assert replacement_view.is_finished() is False
    if edit_fails:
        assert interaction.response.messages[0][0] == (
            "WIN5 화면을 갱신하지 못했습니다. `/win5 submit`을 다시 열어 주세요."
        )
    else:
        assert interaction.response.edits[0]["view"] is replacement_view
        assert interaction.response.messages == []


def test_special_submit_registration_opens_private_layout_editor_through_worker() -> None:
    editor = _special_editor()
    queries = RecordingQueries(special_editor=editor)
    preparation = RecordingPreparation()
    group = Win5MemberCommandGroup(adapter=_adapter(queries, preparation))
    command = group.get_command("special-submit")
    interaction = RecordingInteraction()
    event_loop_thread_id = get_ident()

    assert isinstance(command, app_commands.Command)
    assert command.description == "열린 Special 라운드의 gate 예측을 편집합니다."
    assert len(command.parameters) == 1
    assert command.parameters[0].name == "round_id"
    assert command.parameters[0].autocomplete is True

    asyncio.run(command.callback(group, interaction, 12))  # type: ignore[arg-type,union-attr]

    assert preparation.calls == [(interaction, "win5.special-submit", True)]
    assert queries.special_editor_calls[0][:2] == ("123", 12)
    assert queries.special_editor_calls[0][2] != event_loop_thread_id
    view = interaction.edits[0]["view"]
    assert isinstance(view, Win5SpecialSubmissionEditorView)
    assert "입력: 0 / 7" in _layout_text(view)
    assert "@everyone" not in _layout_text(view)


def test_special_submit_autocomplete_authorizes_bounds_and_disambiguates_titles() -> None:
    choices = (
        Win5SpecialSubmissionRoundChoice(7, "시즌", 12, "Special", 7),
        Win5SpecialSubmissionRoundChoice(7, "시즌", 13, "Special", 7),
    )
    queries = RecordingQueries(special_round_choices=choices)
    authorization = RecordingAutocompleteAuthorization()
    group = Win5MemberCommandGroup(adapter=_adapter(queries, RecordingPreparation(), authorization))
    interaction = RecordingInteraction()

    results = asyncio.run(
        group._special_submission_round_autocomplete(interaction, "Special")  # type: ignore[arg-type]
    )

    assert authorization.calls == [(interaction, "win5.special-submit")]
    assert queries.special_round_search_calls[0][:3] == ("123", "Special", 25)
    assert [(choice.name, choice.value) for choice in results] == [
        ("Special · 7경기 · ID 12", 12),
        ("Special · 7경기 · ID 13", 13),
    ]


def test_special_layout_modal_pages_five_races_prefills_and_applies_local_draft() -> None:
    editor = _special_editor(submission=_accepted_special_submission())
    commands = RecordingCommands()
    authorization = RecordingInteractionAuthorization()
    adapter = _adapter(
        RecordingQueries(),
        RecordingPreparation(),
        commands=commands,
        interaction_authorization=authorization,
    )
    opener = RecordingInteraction()
    context = Win5MemberInteractionContext.from_interaction(opener)
    view = Win5SpecialSubmissionEditorView(
        adapter=adapter,
        context=context,
        editor=editor,
        draft=Win5SpecialSubmissionDraft.from_editor(editor),
    )

    text = _layout_text(view)
    assert "Race 페이지: 1 / 2" in text
    assert "Gate 8 — Reference 1" in text
    assert "Race 3" in text and "Gate 99" in text
    assert "reference" in text
    asyncio.run(
        adapter.open_special_submission_modal(
            opener,  # type: ignore[arg-type]
            context=context,
            editor=editor,
            draft=view.draft,
            source_view=view,
        )
    )

    modal = opener.response.modals[0]
    assert isinstance(modal, Win5SpecialSubmissionModal)
    assert len(modal.children) == 5
    assert all(isinstance(child, discord.ui.TextInput) for child in modal.children)
    assert [text_input.default for _, text_input in modal.fields] == ["8", None, "99", None, None]
    values = ("7", "3", "", "", "10")
    for (_, text_input), value in zip(modal.fields, values, strict=True):
        text_input._value = value

    modal_interaction = RecordingInteraction(interaction_id=556)
    asyncio.run(modal.on_submit(modal_interaction))  # type: ignore[arg-type]

    next_view = modal_interaction.response.edits[0]["view"]
    assert isinstance(next_view, Win5SpecialSubmissionEditorView)
    assert next_view.draft.picks == (
        Win5SpecialSubmissionPick(race_id=1201, gate_number=7),
        Win5SpecialSubmissionPick(race_id=1202, gate_number=3),
        Win5SpecialSubmissionPick(race_id=1205, gate_number=10),
    )
    assert commands.save_calls == []
    assert authorization.calls == [
        (opener, "win5.special-submit"),
        (modal_interaction, "win5.special-submit"),
    ]


def test_special_stale_modal_cannot_overwrite_a_replaced_local_draft() -> None:
    editor = _special_editor()
    authorization = RecordingInteractionAuthorization()
    adapter = _adapter(
        RecordingQueries(),
        RecordingPreparation(),
        interaction_authorization=authorization,
    )
    opener = RecordingInteraction()
    context = Win5MemberInteractionContext.from_interaction(opener)
    source_view = Win5SpecialSubmissionEditorView(
        adapter=adapter,
        context=context,
        editor=editor,
        draft=Win5SpecialSubmissionDraft(),
    )
    replacement_interaction = RecordingInteraction(interaction_id=556)
    asyncio.run(
        adapter.change_special_submission_page(
            replacement_interaction,  # type: ignore[arg-type]
            context=context,
            editor=editor,
            draft=source_view.draft,
            page=1,
            source_view=source_view,
        )
    )
    assert source_view.superseded is True
    interaction = RecordingInteraction(interaction_id=557)

    asyncio.run(
        adapter.apply_special_submission_page(
            interaction,  # type: ignore[arg-type]
            context=context,
            editor=editor,
            draft=source_view.draft,
            values=((1201, "3"), (1202, ""), (1203, ""), (1204, ""), (1205, "")),
            source_view=source_view,
        )
    )

    assert authorization.calls == [
        (replacement_interaction, "win5.special-submit"),
        (interaction, "win5.special-submit"),
    ]
    assert interaction.response.edits == []
    assert "이미 변경된 화면" in interaction.response.messages[0][0]


def test_special_zero_pick_save_is_disabled_and_callback_stops_before_application() -> None:
    editor = _special_editor(submission=_accepted_special_submission())
    draft = Win5SpecialSubmissionDraft()
    commands = RecordingCommands()
    preparation = RecordingPreparation()
    adapter = _adapter(RecordingQueries(), preparation, commands=commands)
    interaction = RecordingInteraction()
    context = Win5MemberInteractionContext.from_interaction(interaction)
    view = Win5SpecialSubmissionEditorView(
        adapter=adapter,
        context=context,
        editor=editor,
        draft=draft,
    )

    buttons = [button for button in _layout_items(view, discord.ui.Button) if isinstance(button, discord.ui.Button)]
    assert next(button for button in buttons if button.label == "변경 저장").disabled is True
    assert next(button for button in buttons if button.label == "제출 취소").disabled is False

    asyncio.run(
        adapter.save_special_submission(
            interaction,  # type: ignore[arg-type]
            context=context,
            editor=editor,
            draft=draft,
            source_view=view,
        )
    )

    assert preparation.calls == []
    assert commands.save_calls == []
    assert "하나 이상의 pick" in interaction.response.messages[0][0]
    assert view.is_finished() is False


def test_special_versioned_save_translates_full_gate_draft_without_reference_restriction() -> None:
    editor = _special_editor(submission=_accepted_special_submission())
    draft = Win5SpecialSubmissionDraft(
        picks=(
            Win5SpecialSubmissionPick(race_id=1201, gate_number=8),
            Win5SpecialSubmissionPick(race_id=1202, gate_number=42),
        )
    )
    result = SavedWin5Submission(
        season_id=7,
        round_id=12,
        submission_id=502,
        persona_id="persona-1",
        tier=Win5SubmissionTier.SPECIAL_WINNER,
        status=Win5SubmissionStatus.ACCEPTED,
        version=7,
        picks=(
            Win5SubmissionPickAuditSnapshot(1201, 1, None, 8),
            Win5SubmissionPickAuditSnapshot(1202, 1, None, 42),
        ),
    )
    commands = RecordingCommands(save_result=result)
    preparation = RecordingPreparation()
    adapter = _adapter(RecordingQueries(), preparation, commands=commands)
    interaction = RecordingInteraction(interaction_id=777)
    context = Win5MemberInteractionContext.from_interaction(interaction)
    source_view = Win5SpecialSubmissionEditorView(
        adapter=adapter,
        context=context,
        editor=editor,
        draft=draft,
    )
    original_save = commands.save_submission
    source_superseded_at_command: list[bool] = []

    def record_save(command: object) -> SavedWin5Submission:
        source_superseded_at_command.append(source_view.superseded)
        return original_save(command)

    commands.save_submission = record_save  # type: ignore[method-assign]
    event_loop_thread_id = get_ident()

    asyncio.run(
        adapter.save_special_submission(
            interaction,  # type: ignore[arg-type]
            context=context,
            editor=editor,
            draft=draft,
            source_view=source_view,
        )
    )

    assert preparation.calls == [(interaction, "win5.special-submit", True)]
    assert source_superseded_at_command == [True]
    command, worker_thread_id = commands.save_calls[0]
    assert worker_thread_id != event_loop_thread_id
    assert command.round_id == 12
    assert command.tier == Win5SubmissionTier.SPECIAL_WINNER
    assert command.submission_id == 502
    assert command.expected_version == 6
    assert [(pick.race_id, pick.position, pick.race_entry_id, pick.gate_number) for pick in command.picks] == [
        (1201, 1, None, 8),
        (1202, 1, None, 42),
    ]
    next_view = interaction.edits[0]["view"]
    assert isinstance(next_view, Win5SpecialSubmissionEditorView)
    assert next_view.editor.submission is not None
    assert next_view.editor.submission.version == 7
    receipt = _layout_text(next_view)
    assert "접수 성공 · version 7" in receipt
    assert "Race 1" in receipt and "Gate 8 — Reference 1" in receipt
    assert "Race 2" in receipt and "Gate 42" in receipt
    assert "Race 3" in receipt and "Race 3 @\u200beveryone: —" in receipt

    duplicate = RecordingInteraction(interaction_id=778)
    asyncio.run(
        adapter.save_special_submission(
            duplicate,  # type: ignore[arg-type]
            context=context,
            editor=editor,
            draft=draft,
            source_view=source_view,
        )
    )

    assert len(commands.save_calls) == 1
    assert "이미 시작" in duplicate.response.messages[0][0]


def test_special_explicit_cancel_confirmation_uses_versioned_command() -> None:
    editor = _special_editor(submission=_accepted_special_submission())
    result = CancelledWin5Submission(
        season_id=7,
        round_id=12,
        submission_id=502,
        persona_id="persona-1",
        tier=Win5SubmissionTier.SPECIAL_WINNER,
        status=Win5SubmissionStatus.CANCELLED,
        version=7,
    )
    commands = RecordingCommands(cancel_result=result)
    authorization = RecordingInteractionAuthorization()
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        preparation,
        commands=commands,
        interaction_authorization=authorization,
    )
    interaction = RecordingInteraction(interaction_id=888)
    context = Win5MemberInteractionContext.from_interaction(interaction)
    draft = Win5SpecialSubmissionDraft.from_editor(editor)
    editor_view = Win5SpecialSubmissionEditorView(
        adapter=adapter,
        context=context,
        editor=editor,
        draft=draft,
    )

    asyncio.run(
        adapter.show_special_submission_cancel_confirmation(
            interaction,  # type: ignore[arg-type]
            context=context,
            editor=editor,
            draft=draft,
            source_view=editor_view,
        )
    )
    confirm_view = interaction.response.edits[0]["view"]
    assert isinstance(confirm_view, Win5SpecialSubmissionCancelConfirmView)
    assert authorization.calls == [(interaction, "win5.special-submit")]
    original_stop = confirm_view.stop
    stop_calls: list[bool] = []

    def record_stop() -> None:
        stop_calls.append(True)
        original_stop()

    confirm_view.stop = record_stop  # type: ignore[method-assign]
    original_cancel = commands.cancel_submission
    source_finished_at_command: list[bool] = []

    def record_cancel(command: object) -> CancelledWin5Submission:
        source_finished_at_command.append(bool(stop_calls))
        return original_cancel(command)

    commands.cancel_submission = record_cancel  # type: ignore[method-assign]

    asyncio.run(
        adapter.cancel_special_submission(
            interaction,  # type: ignore[arg-type]
            context=context,
            editor=editor,
            source_view=confirm_view,
        )
    )

    command, _ = commands.cancel_calls[0]
    assert command.submission_id == 502
    assert command.expected_version == 6
    assert source_finished_at_command == [True]
    assert preparation.calls == [(interaction, "win5.special-submit", True)]
    assert "Special 제출 취소 완료" in _layout_text(interaction.edits[0]["view"])

    duplicate = RecordingInteraction(interaction_id=889)
    asyncio.run(
        adapter.cancel_special_submission(
            duplicate,  # type: ignore[arg-type]
            context=context,
            editor=editor,
            source_view=confirm_view,
        )
    )

    assert len(commands.cancel_calls) == 1
    assert "이미 시작" in duplicate.response.messages[0][0]


def test_cancel_autocomplete_authorizes_bounds_and_disambiguates_titles() -> None:
    targets = (
        _cancellable_submission(submission_id=501, round_id=11, round_name="Round 11"),
        _cancellable_submission(submission_id=502, round_id=12, round_name="Round 11"),
    )
    queries = RecordingQueries(cancellable_submissions=targets)
    authorization = RecordingAutocompleteAuthorization()
    group = Win5MemberCommandGroup(adapter=_adapter(queries, RecordingPreparation(), authorization))
    interaction = RecordingInteraction()
    event_loop_thread_id = get_ident()

    results = asyncio.run(
        group._cancellable_submission_autocomplete(interaction, "Round")  # type: ignore[arg-type]
    )

    assert authorization.calls == [(interaction, "win5.cancel")]
    assert queries.cancellable_search_calls[0][:3] == ("123", "Round", 25)
    assert queries.cancellable_search_calls[0][3] != event_loop_thread_id
    assert [(choice.name, choice.value) for choice in results] == [
        ("Round 11 · TOP3 · ID 501", 501),
        ("Round 11 · TOP3 · ID 502", 502),
    ]


def test_cancel_registration_opens_private_bound_confirmation_through_worker() -> None:
    target = _cancellable_submission()
    queries = RecordingQueries(cancellable_submission=target)
    preparation = RecordingPreparation()
    group = Win5MemberCommandGroup(adapter=_adapter(queries, preparation))
    command = group.get_command("cancel")
    interaction = RecordingInteraction()
    event_loop_thread_id = get_ident()

    assert isinstance(command, app_commands.Command)
    assert command.description == "열린 라운드의 내 WIN5 제출을 취소합니다."
    assert [parameter.name for parameter in command.parameters] == ["submission_id", "reason"]
    assert command.parameters[0].required is True
    assert command.parameters[0].autocomplete is True
    assert command.parameters[1].required is False

    asyncio.run(
        command.callback(group, interaction, 501, "  사용자 요청 @everyone  ")  # type: ignore[arg-type,union-attr]
    )

    assert preparation.calls == [(interaction, "win5.cancel", True)]
    assert queries.cancellable_calls[0][:2] == ("123", 501)
    assert queries.cancellable_calls[0][2] != event_loop_thread_id
    assert len(interaction.edits) == 1
    view = interaction.edits[0]["view"]
    assert isinstance(view, Win5SubmissionCancelConfirmView)
    text = _layout_text(view)
    assert "WIN5 제출 취소 확인" in text
    assert "Round 11" in text
    assert "사용자 요청" in text
    assert "@everyone" not in text


def test_cancel_confirmation_executes_exact_version_and_optional_reason() -> None:
    target = _cancellable_submission()
    result = CancelledWin5Submission(
        season_id=7,
        round_id=11,
        submission_id=501,
        persona_id="persona-1",
        tier=Win5SubmissionTier.TOP3,
        status=Win5SubmissionStatus.CANCELLED,
        version=5,
    )
    queries = RecordingQueries(cancellable_submission=target)
    commands = RecordingCommands(cancel_result=result)
    preparation = RecordingPreparation()
    group = Win5MemberCommandGroup(adapter=_adapter(queries, preparation, commands=commands))
    opener = RecordingInteraction(interaction_id=555)
    command = group.get_command("cancel")

    asyncio.run(
        command.callback(group, opener, 501, "사용자 요청")  # type: ignore[arg-type,union-attr]
    )
    view = opener.edits[0]["view"]
    assert isinstance(view, Win5SubmissionCancelConfirmView)
    confirm = next(button for button in _layout_items(view, discord.ui.Button) if button.label == "제출 취소 확정")
    original_cancel = commands.cancel_submission
    source_finished_at_command: list[bool] = []

    def record_cancel(command: object) -> CancelledWin5Submission:
        source_finished_at_command.append(view.is_finished())
        return original_cancel(command)

    commands.cancel_submission = record_cancel  # type: ignore[method-assign]
    interaction = RecordingInteraction(interaction_id=777)
    event_loop_thread_id = get_ident()

    asyncio.run(confirm.callback(interaction))  # type: ignore[attr-defined]

    submitted, worker_thread_id = commands.cancel_calls[0]
    assert submitted.round_id == 11
    assert submitted.submission_id == 501
    assert submitted.expected_version == 4
    assert submitted.actor_discord_user_id == "123"
    assert submitted.guild_id == "987"
    assert submitted.correlation_id == "777"
    assert submitted.reason == "사용자 요청"
    assert source_finished_at_command == [True]
    assert worker_thread_id != event_loop_thread_id
    assert preparation.calls == [
        (opener, "win5.cancel", True),
        (interaction, "win5.cancel", True),
    ]
    assert "WIN5 제출 취소 완료" in _layout_text(interaction.edits[0]["view"])
    assert "version: 5" in _layout_text(interaction.edits[0]["view"])

    duplicate = RecordingInteraction(interaction_id=778)
    asyncio.run(confirm.callback(duplicate))  # type: ignore[attr-defined]

    assert len(commands.cancel_calls) == 1
    assert "이미 시작" in duplicate.response.messages[0][0]


def test_cancel_stale_target_and_context_mismatch_stop_before_mutation() -> None:
    target = _cancellable_submission()
    queries = RecordingQueries(cancellable_submission=target)
    commands = RecordingCommands(error=Win5SubmissionVersionConflictError("internal version=9"))
    preparation = RecordingPreparation()
    group = Win5MemberCommandGroup(adapter=_adapter(queries, preparation, commands=commands))
    opener = RecordingInteraction()
    command = group.get_command("cancel")

    asyncio.run(command.callback(group, opener, 501, None))  # type: ignore[arg-type,union-attr]
    view = opener.edits[0]["view"]
    confirm = next(button for button in _layout_items(view, discord.ui.Button) if button.label == "제출 취소 확정")
    mismatched = RecordingInteraction(user_id=999)

    asyncio.run(confirm.callback(mismatched))  # type: ignore[attr-defined]

    assert commands.cancel_calls == []
    assert preparation.calls == [(opener, "win5.cancel", True)]
    assert mismatched.response.messages[0][1]["ephemeral"] is True

    interaction = RecordingInteraction(interaction_id=778)
    asyncio.run(confirm.callback(interaction))  # type: ignore[attr-defined]

    assert len(commands.cancel_calls) == 1
    terminal = _layout_text(interaction.edits[0]["view"])
    assert "Submission이 변경" in terminal
    assert "`/win5 cancel`" in terminal
    assert "version=9" not in terminal


def test_cancel_unavailable_target_uses_fixed_private_message() -> None:
    queries = RecordingQueries(cancellable_error=Win5CancellableSubmissionUnavailableError("internal submission=501"))
    preparation = RecordingPreparation()
    group = Win5MemberCommandGroup(adapter=_adapter(queries, preparation))
    command = group.get_command("cancel")
    interaction = RecordingInteraction()

    asyncio.run(command.callback(group, interaction, 501, None))  # type: ignore[arg-type,union-attr]

    assert interaction.edits[0]["content"] == (
        "WIN5 제출 취소 화면을 열지 못했습니다: 선택한 accepted Submission이 더 이상 열린 상태가 아닙니다."
    )
    assert "submission=501" not in interaction.edits[0]["content"]
