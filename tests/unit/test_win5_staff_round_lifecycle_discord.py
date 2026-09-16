"""Discord adapter tests for WIN5 staff Round lifecycle workflows."""

from __future__ import annotations

from win5_staff_discord_test_support import (
    RecordingAuthorization,
    RecordingDeletionCommands,
    RecordingDeletionQueries,
    RecordingInteraction,
    RecordingLifecycleCommands,
    RecordingLifecycleQueries,
    RecordingPreparation,
    RecordingQueries,
    Win5NormalResultTargetMode,
    Win5RoundLifecycleAction,
    Win5RoundLifecycleConfirmView,
    Win5RoundLifecyclePreview,
    Win5RoundLifecycleTargetPage,
    Win5RoundLifecycleTargetView,
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SetupRoundDeletionConfirmView,
    Win5SetupRoundDeletionPreview,
    Win5SetupRoundDeletionRace,
    Win5SetupRoundDeletionReasonModal,
    Win5SetupRoundDeletionSnapshot,
    Win5SetupRoundDeletionTargetChoice,
    Win5SetupRoundDeletionTargetPage,
    Win5SetupRoundDeletionTargetView,
    Win5StaffInteractionContext,
    Win5StaffRoundActionView,
    _adapter,
    _choices,
    _deleted_round,
    _deletion_snapshot,
    _lifecycle_choices,
    _lifecycle_target,
    _transitioned_round,
    asyncio,
    discord,
    format_round_lifecycle_preview,
    format_setup_round_deletion_preview,
    get_ident,
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


def test_setup_round_deletion_action_uses_worker_query_and_paged_selector() -> None:
    page = Win5SetupRoundDeletionTargetPage(
        offset=0,
        limit=25,
        choices=tuple(
            Win5SetupRoundDeletionTargetChoice(
                season_id=7,
                season_name="2026 @everyone 하반기",
                season_status=Win5SeasonStatus.ACTIVE,
                round_id=round_id,
                round_name=f"삭제 Round {round_id}",
                round_type=Win5RoundType.SPECIAL,
                round_status=Win5RoundStatus.SETUP,
                race_count=2,
                entry_count=0,
            )
            for round_id in range(11, 36)
        ),
        has_next=True,
    )
    deletion_queries = RecordingDeletionQueries(page=page)
    authorization = RecordingAuthorization()
    adapter = _adapter(
        RecordingQueries(),
        deletion_queries=deletion_queries,
        authorization=authorization,
    )
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5StaffRoundActionView(adapter=adapter, context=context)
    select = source_view.children[0]
    select._values = ["round-delete"]
    stop_calls, stopped_at_edit = _track_stop_before_edit(source_view, interaction)
    event_loop_thread_id = get_ident()

    asyncio.run(select.callback(interaction))  # type: ignore[arg-type]

    assert authorization.calls == [(interaction, "win5.staff.round")]
    assert deletion_queries.list_calls[0][:2] == (0, 25)
    assert deletion_queries.list_calls[0][2] != event_loop_thread_id
    assert stop_calls == [True]
    assert stopped_at_edit == [True]
    edit = interaction.response.edits[0]
    assert "Round와 모든 Race/Entry가 삭제됩니다" in edit["content"]
    target_view = edit["view"]
    assert isinstance(target_view, Win5SetupRoundDeletionTargetView)
    assert len(target_view.children) == 2
    assert [option.value for option in target_view.children[0].options] == [str(value) for value in range(11, 36)]
    assert target_view.children[1].label == "다음"


def test_setup_round_deletion_selection_opens_required_reason_modal_without_defer() -> None:
    authorization = RecordingAuthorization()
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        authorization=authorization,
        preparation=preparation,
    )
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    page = RecordingDeletionQueries().list_setup_round_deletion_targets(offset=0, limit=25)
    view = Win5SetupRoundDeletionTargetView(adapter=adapter, context=context, page=page)
    stop_calls = _track_stop(view)
    select = view.children[0]
    select._values = ["11"]

    asyncio.run(select.callback(interaction))  # type: ignore[arg-type]

    assert authorization.calls == [(interaction, "win5.staff.round")]
    assert preparation.calls == []
    assert len(interaction.response.modals) == 1
    modal = interaction.response.modals[0]
    assert isinstance(modal, Win5SetupRoundDeletionReasonModal)
    assert len(modal.children) == 1
    assert modal.children[0].required is True
    assert modal.children[0].max_length == 255
    assert modal._source_view is view
    assert stop_calls == []


def test_setup_round_deletion_reason_submit_requeries_and_renders_destructive_preview() -> None:
    snapshot = _deletion_snapshot()
    deletion_queries = RecordingDeletionQueries(target=snapshot)
    deletion_commands = RecordingDeletionCommands()
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        deletion_queries=deletion_queries,
        deletion_commands=deletion_commands,
        preparation=preparation,
    )
    interaction = RecordingInteraction(interaction_id=998)
    page = RecordingDeletionQueries().list_setup_round_deletion_targets(offset=0, limit=25)
    source_view = Win5SetupRoundDeletionTargetView(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(interaction),
        page=page,
    )
    modal = Win5SetupRoundDeletionReasonModal(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(interaction),
        round_id=11,
        source_view=source_view,
    )
    modal.reason._value = " 게이트 오입력으로 재생성 "
    stop_calls, stopped_at_edit = _track_stop_before_edit(
        source_view,
        interaction,
        deferred=True,
    )
    event_loop_thread_id = get_ident()

    asyncio.run(modal.on_submit(interaction))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "win5.staff.round", True)]
    assert deletion_queries.get_calls[0][0] == 11
    assert deletion_queries.get_calls[0][1] != event_loop_thread_id
    assert deletion_commands.calls == []
    assert stop_calls == [True]
    assert stopped_at_edit == [True]
    edit = interaction.edits[0]
    assert "WIN5 setup 라운드 영구 삭제 확인" in edit["content"]
    assert "Round 1개 · Race 1개 · Entry 5개" in edit["content"]
    assert "운영 사유: 게이트 오입력으로 재생성" in edit["content"]
    assert "자동 생성되지 않습니다" in edit["content"]
    assert "@everyone" not in edit["content"]
    assert isinstance(edit["view"], Win5SetupRoundDeletionConfirmView)


def test_setup_round_deletion_confirmation_uses_fingerprint_reason_and_final_interaction_id() -> None:
    snapshot = _deletion_snapshot()
    preview = Win5SetupRoundDeletionPreview(snapshot=snapshot, reason="게이트 오입력으로 재생성")
    deletion_commands = RecordingDeletionCommands(result=_deleted_round())
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        deletion_commands=deletion_commands,
        preparation=preparation,
    )
    interaction = RecordingInteraction(interaction_id=999)
    view = Win5SetupRoundDeletionConfirmView(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(interaction),
        preview=preview,
    )
    stop_calls = _track_stop(view)
    original_delete_round = deletion_commands.delete_round

    def delete_round_after_view_closed(command: object) -> object:
        assert stop_calls == [True]
        return original_delete_round(command)  # type: ignore[arg-type]

    deletion_commands.delete_round = delete_round_after_view_closed  # type: ignore[method-assign]
    event_loop_thread_id = get_ident()

    asyncio.run(view.children[0].callback(interaction))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "win5.staff.round", True)]
    command, worker_thread_id = deletion_commands.calls[0]
    assert worker_thread_id != event_loop_thread_id
    assert command.season_id == 7
    assert command.round_id == 11
    assert command.expected_graph_fingerprint == snapshot.graph_fingerprint
    assert command.idempotency_key == "win5-setup-round-delete:999"
    assert command.actor_discord_user_id == "123"
    assert command.guild_id == "987"
    assert command.correlation_id == "999"
    assert command.reason == "게이트 오입력으로 재생성"
    assert "WIN5 setup 라운드를 삭제했습니다" in interaction.edits[0]["content"]
    assert "@everyone" not in interaction.edits[0]["content"]
    assert interaction.edits[0]["view"] is None
    assert stop_calls == [True]


def test_setup_round_deletion_confirmation_is_claimed_once_per_preview() -> None:
    deletion_commands = RecordingDeletionCommands(result=_deleted_round())
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        deletion_commands=deletion_commands,
        preparation=preparation,
    )
    opening = RecordingInteraction(interaction_id=998)
    view = Win5SetupRoundDeletionConfirmView(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(opening),
        preview=Win5SetupRoundDeletionPreview(
            snapshot=_deletion_snapshot(),
            reason="게이트 오입력으로 재생성",
        ),
    )
    retry = RecordingInteraction(interaction_id=999)

    asyncio.run(view.children[0].callback(opening))  # type: ignore[arg-type]
    asyncio.run(view.children[0].callback(retry))  # type: ignore[arg-type]

    assert len(deletion_commands.calls) == 1
    assert "이미 처리 중이거나 완료" in retry.response.messages[0][0]


def test_setup_round_deletion_confirm_context_mismatch_and_cancel_are_zero_write() -> None:
    deletion_commands = RecordingDeletionCommands(result=_deleted_round())
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        deletion_commands=deletion_commands,
        preparation=preparation,
    )
    opening = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(opening)
    view = Win5SetupRoundDeletionConfirmView(
        adapter=adapter,
        context=context,
        preview=Win5SetupRoundDeletionPreview(
            snapshot=_deletion_snapshot(),
            reason="게이트 오입력으로 재생성",
        ),
    )
    other = RecordingInteraction(user_id=456)

    asyncio.run(view.children[0].callback(other))  # type: ignore[arg-type]
    asyncio.run(view.children[1].callback(opening))  # type: ignore[arg-type]

    assert deletion_commands.calls == []
    assert preparation.calls == []
    assert "연 사용자와 서버·채널" in other.response.messages[0][0]
    assert opening.response.edits[0]["content"] == "WIN5 setup 라운드 삭제를 취소했습니다."
    assert opening.response.edits[0]["view"] is None


def test_setup_round_deletion_selector_failure_closes_source_and_sends_reopen_notice() -> None:
    adapter = _adapter(RecordingQueries())
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5StaffRoundActionView(adapter=adapter, context=context)
    stop_calls, stopped_at_edit = _track_stop_before_edit(source_view, interaction, fail=True)

    asyncio.run(
        adapter.show_setup_round_deletion_targets(
            interaction,  # type: ignore[arg-type]
            context=context,
            offset=0,
            source_view=source_view,
        )
    )

    assert stop_calls == [True]
    assert stopped_at_edit == [True]
    assert "`/win5 staff round`를 다시 열어" in interaction.response.messages[0][0]


def test_setup_round_deletion_preview_failure_closes_source_and_sends_reopen_notice() -> None:
    adapter = _adapter(
        RecordingQueries(),
        deletion_queries=RecordingDeletionQueries(target=_deletion_snapshot()),
    )
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    page = RecordingDeletionQueries().list_setup_round_deletion_targets(offset=0, limit=25)
    source_view = Win5SetupRoundDeletionTargetView(adapter=adapter, context=context, page=page)
    stop_calls, stopped_at_edit = _track_stop_before_edit(
        source_view,
        interaction,
        deferred=True,
        fail=True,
    )

    asyncio.run(
        adapter.preview_setup_round_deletion(
            interaction,  # type: ignore[arg-type]
            context=context,
            round_id=11,
            reason="재생성",
            source_view=source_view,
        )
    )

    assert stop_calls == [True]
    assert stopped_at_edit == [True]
    assert "`/win5 staff round`를 다시 열어" in interaction.response.messages[0][0]


def test_setup_round_deletion_cancel_failure_still_closes_source() -> None:
    adapter = _adapter(RecordingQueries())
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    view = Win5SetupRoundDeletionConfirmView(
        adapter=adapter,
        context=context,
        preview=Win5SetupRoundDeletionPreview(
            snapshot=_deletion_snapshot(),
            reason="재생성",
        ),
    )
    stop_calls, stopped_at_edit = _track_stop_before_edit(view, interaction, fail=True)

    asyncio.run(view.children[1].callback(interaction))  # type: ignore[arg-type]

    assert stop_calls == [True]
    assert stopped_at_edit == [True]


def test_setup_round_deletion_formatter_is_bounded_and_mention_safe() -> None:
    message = format_setup_round_deletion_preview(
        Win5SetupRoundDeletionPreview(
            snapshot=_deletion_snapshot(),
            reason="오입력 @everyone",
        )
    )

    assert len(message) <= 1900
    assert "@everyone" not in message
    assert "Round와 모든 Race/Entry가 삭제됩니다" in message

    large_snapshot = Win5SetupRoundDeletionSnapshot(
        season_id=7,
        season_name="2026 하반기",
        season_status=Win5SeasonStatus.ACTIVE,
        round_id=11,
        round_name="대형 Special Round",
        round_type=Win5RoundType.SPECIAL,
        round_status=Win5RoundStatus.SETUP,
        source_kind=Win5RoundSourceKind.NATIVE_V2,
        races=tuple(
            Win5SetupRoundDeletionRace(
                id=100 + index,
                name=f"Race {index} " + "긴 이름 " * 30,
            )
            for index in range(1, 21)
        ),
    )
    large_message = format_setup_round_deletion_preview(
        Win5SetupRoundDeletionPreview(snapshot=large_snapshot, reason="전체 재생성")
    )

    assert len(large_message) <= 1900
    assert "확정하면 Round와 모든 Race/Entry가 삭제됩니다" in large_message
    assert "새 Round는 자동 생성되지 않습니다" in large_message


def test_lifecycle_action_uses_worker_query_and_paged_round_selector() -> None:
    page = Win5RoundLifecycleTargetPage(
        action=Win5RoundLifecycleAction.OPEN,
        offset=0,
        limit=25,
        choices=_lifecycle_choices(count=25),
        has_next=True,
    )
    lifecycle_queries = RecordingLifecycleQueries(page=page)
    authorization = RecordingAuthorization()
    adapter = _adapter(
        RecordingQueries(),
        lifecycle_queries=lifecycle_queries,
        authorization=authorization,
    )
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5StaffRoundActionView(adapter=adapter, context=context)
    select = source_view.children[0]
    select._values = ["round-open"]
    event_loop_thread_id = get_ident()
    stop_calls, source_stopped_at_edit = _track_stop_before_edit(source_view, interaction)

    asyncio.run(select.callback(interaction))  # type: ignore[attr-defined,arg-type]

    assert source_stopped_at_edit == [True]
    assert stop_calls == [True]
    assert authorization.calls == [(interaction, "win5.staff.round")]
    assert lifecycle_queries.list_calls[0][:3] == (Win5RoundLifecycleAction.OPEN, 0, 25)
    assert lifecycle_queries.list_calls[0][3] != event_loop_thread_id
    edit = interaction.response.edits[0]
    assert edit["content"] == "열기 대상 WIN5 Round를 선택해 주세요. (페이지 1)"
    target_view = edit["view"]
    assert isinstance(target_view, Win5RoundLifecycleTargetView)
    assert len(target_view.children) == 2
    assert [option.value for option in target_view.children[0].options] == [str(value) for value in range(11, 36)]
    assert target_view.children[1].label == "다음"


def test_lifecycle_target_selection_requeries_and_renders_zero_write_preview() -> None:
    target = _lifecycle_target()
    lifecycle_queries = RecordingLifecycleQueries(target=target)
    lifecycle_commands = RecordingLifecycleCommands()
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        lifecycle_queries=lifecycle_queries,
        lifecycle_commands=lifecycle_commands,
        preparation=preparation,
    )
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    page = Win5RoundLifecycleTargetPage(
        action=Win5RoundLifecycleAction.OPEN,
        offset=0,
        limit=25,
        choices=(_lifecycle_choices(count=1)[0],),
    )
    view = Win5RoundLifecycleTargetView(adapter=adapter, context=context, page=page)
    select = view.children[0]
    select._values = ["11"]
    event_loop_thread_id = get_ident()
    stop_calls, source_stopped_at_edit = _track_stop_before_edit(
        view,
        interaction,
        deferred=True,
    )

    asyncio.run(select.callback(interaction))  # type: ignore[attr-defined,arg-type]

    assert source_stopped_at_edit == [True]
    assert stop_calls == [True]
    assert preparation.calls == [(interaction, "win5.staff.round", True)]
    assert lifecycle_queries.get_calls[0][:2] == (11, Win5RoundLifecycleAction.OPEN)
    assert lifecycle_queries.get_calls[0][2] != event_loop_thread_id
    assert lifecycle_commands.calls == []
    edit = interaction.edits[0]
    assert edit["content"] == (
        "WIN5 라운드 열기 확인\n"
        "시즌: 2026 @\u200beveryone 하반기\n"
        "라운드: 제3회 @\u200beveryone 아리마 기념\n"
        "유형: Normal\n"
        "현재 상태: setup → open\n"
        "Race: 1개\n"
        "Entry: 6개\n"
        "현재 open Round: 4/25개\n"
        "확정하면 이 Round의 참가자 제출을 받기 시작합니다."
    )
    assert isinstance(edit["view"], Win5RoundLifecycleConfirmView)


def test_lifecycle_confirmation_uses_final_interaction_id_and_one_round_command() -> None:
    target = _lifecycle_target()
    preview = Win5RoundLifecyclePreview(target=target)
    lifecycle_commands = RecordingLifecycleCommands(result=_transitioned_round())
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        lifecycle_commands=lifecycle_commands,
        preparation=preparation,
    )
    interaction = RecordingInteraction(interaction_id=999)
    view = Win5RoundLifecycleConfirmView(
        adapter=adapter,
        context=Win5StaffInteractionContext.from_interaction(interaction),
        preview=preview,
    )
    event_loop_thread_id = get_ident()
    stop_calls = _track_stop(view)

    asyncio.run(view.children[0].callback(interaction))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "win5.staff.round", True)]
    command, worker_thread_id = lifecycle_commands.calls[0]
    assert worker_thread_id != event_loop_thread_id
    assert command.round_id == 11
    assert command.action == Win5RoundLifecycleAction.OPEN
    assert command.idempotency_key == "win5-round-open:999"
    assert command.actor_discord_user_id == "123"
    assert command.guild_id == "987"
    assert command.correlation_id == "999"
    assert interaction.edits[0]["content"] == (
        "WIN5 라운드를 열었습니다.\n"
        "시즌: 2026 @\u200beveryone 하반기\n"
        "라운드: 제3회 @\u200beveryone 아리마 기념\n"
        "상태: setup → open"
    )
    assert interaction.edits[0]["view"] is None
    assert stop_calls == [True]

    duplicate = RecordingInteraction(interaction_id=1000)
    asyncio.run(view.children[0].callback(duplicate))  # type: ignore[arg-type]

    assert len(lifecycle_commands.calls) == 1
    assert "이미 처리 중이거나 완료" in duplicate.response.messages[0][0]


def test_lifecycle_target_replacement_failure_stops_source_and_prompts_reopen() -> None:
    page = Win5RoundLifecycleTargetPage(
        action=Win5RoundLifecycleAction.OPEN,
        offset=0,
        limit=25,
        choices=_lifecycle_choices(count=1),
    )
    adapter = _adapter(
        RecordingQueries(),
        lifecycle_queries=RecordingLifecycleQueries(page=page),
    )
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5StaffRoundActionView(adapter=adapter, context=context)
    stop_calls, source_stopped_at_edit = _track_stop_before_edit(
        source_view,
        interaction,
        fail=True,
    )

    asyncio.run(
        adapter.show_round_lifecycle_targets(
            interaction,
            context=context,
            action=Win5RoundLifecycleAction.OPEN,
            offset=0,
            source_view=source_view,
        )
    )

    assert source_stopped_at_edit == [True]
    assert stop_calls == [True]
    assert interaction.response.messages[-1][0] == (
        "WIN5 Round 상태 변경 화면을 갱신하지 못했습니다. `/win5 staff round`를 다시 열어 주세요."
    )


def test_lifecycle_deferred_preview_failure_stops_source_and_uses_followup() -> None:
    target = _lifecycle_target()
    adapter = _adapter(
        RecordingQueries(),
        lifecycle_queries=RecordingLifecycleQueries(target=target),
    )
    interaction = RecordingInteraction()
    interaction.response.done = True
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = discord.ui.View()
    stop_calls, source_stopped_at_edit = _track_stop_before_edit(
        source_view,
        interaction,
        deferred=True,
        fail=True,
    )

    asyncio.run(
        adapter.preview_round_lifecycle(
            interaction,
            context=context,
            action=Win5RoundLifecycleAction.OPEN,
            round_id=target.round_id,
            source_view=source_view,
        )
    )

    assert source_stopped_at_edit == [True]
    assert stop_calls == [True]
    assert interaction.followup.messages[-1][0] == (
        "WIN5 Round 상태 변경 화면을 갱신하지 못했습니다. `/win5 staff round`를 다시 열어 주세요."
    )


def test_lifecycle_final_delivery_failure_still_closes_source_after_command() -> None:
    lifecycle_commands = RecordingLifecycleCommands(result=_transitioned_round())
    adapter = _adapter(RecordingQueries(), lifecycle_commands=lifecycle_commands)
    interaction = RecordingInteraction(interaction_id=1001)
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5RoundLifecycleConfirmView(
        adapter=adapter,
        context=context,
        preview=Win5RoundLifecyclePreview(target=_lifecycle_target()),
    )
    stop_calls = _track_stop(source_view)

    async def fail_delivery(**_kwargs: object) -> None:
        raise RuntimeError("Discord terminal edit failed")

    interaction.edit_original_response = fail_delivery  # type: ignore[method-assign]

    asyncio.run(source_view.children[0].callback(interaction))  # type: ignore[arg-type]

    assert len(lifecycle_commands.calls) == 1
    assert stop_calls == [True]


def test_lifecycle_cancel_delivery_failure_still_closes_source_without_write() -> None:
    lifecycle_commands = RecordingLifecycleCommands(result=_transitioned_round())
    adapter = _adapter(RecordingQueries(), lifecycle_commands=lifecycle_commands)
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5RoundLifecycleConfirmView(
        adapter=adapter,
        context=context,
        preview=Win5RoundLifecyclePreview(target=_lifecycle_target()),
    )
    stop_calls, source_stopped_at_edit = _track_stop_before_edit(
        source_view,
        interaction,
        fail=True,
    )

    asyncio.run(source_view.children[1].callback(interaction))  # type: ignore[arg-type]

    assert lifecycle_commands.calls == []
    assert source_stopped_at_edit == [True]
    assert stop_calls == [True]


def test_close_preview_reports_submission_freeze_without_mutation() -> None:
    message = format_round_lifecycle_preview(
        Win5RoundLifecyclePreview(
            target=_lifecycle_target(action=Win5RoundLifecycleAction.CLOSE),
        )
    )

    assert "현재 상태: open → closed" in message
    assert "현재 accepted Submission: 3건" in message
    assert "이후 수정·취소를 받지 않습니다" in message
    assert "@everyone" not in message


def test_action_selection_uses_worker_query_and_operator_titles_with_duplicate_id_suffix() -> None:
    queries = RecordingQueries(choices=_choices())
    authorization = RecordingAuthorization()
    adapter = _adapter(queries, authorization=authorization)
    original = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(original)
    action_view = discord.ui.View()
    from uma_st2.adapters.discord.win5_staff import Win5StaffRoundActionSelect

    select = Win5StaffRoundActionSelect(adapter=adapter, context=context)
    action_view.add_item(select)
    select._values = ["entry"]
    interaction = RecordingInteraction()
    event_loop_thread_id = get_ident()

    asyncio.run(select.callback(interaction))  # type: ignore[arg-type]

    assert authorization.calls == [(interaction, "win5.staff.round")]
    assert queries.list_calls[0][:2] == (Win5NormalResultTargetMode.ENTRY, 25)
    assert queries.list_calls[0][2] != event_loop_thread_id
    target_view = interaction.response.edits[0]["view"]
    target_select = target_view.children[0]
    assert [option.value for option in target_select.options] == ["11", "12"]
    assert [option.label for option in target_select.options] == [
        "같은 제목 @\u200beveryone · 같은 경기 · ID 11",
        "같은 제목 @\u200beveryone · 같은 경기 · ID 12",
    ]
