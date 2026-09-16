"""Discord adapter tests for WIN5 staff Season workflows."""

from __future__ import annotations

from win5_staff_discord_test_support import (
    UTC,
    RecordingAuthorization,
    RecordingInteraction,
    RecordingPreparation,
    RecordingQueries,
    RecordingSeasonCommands,
    RecordingSeasonQueries,
    Win5SeasonAction,
    Win5SeasonConfirmView,
    Win5SeasonMetadataModal,
    Win5SeasonMutationPreview,
    Win5SeasonRoundState,
    Win5SeasonTargetPage,
    Win5SeasonTargetView,
    Win5StaffCommandGroup,
    Win5StaffInteractionContext,
    Win5StaffSeasonActionView,
    _adapter,
    _changed_season,
    _season_snapshot,
    app_commands,
    asyncio,
    datetime,
    discord,
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


def test_staff_season_registration_opens_private_five_action_panel() -> None:
    authorization = RecordingAuthorization()
    adapter = _adapter(RecordingQueries(), authorization=authorization)
    group = Win5StaffCommandGroup(adapter=adapter)
    command = group.get_command("season")
    interaction = RecordingInteraction()

    assert isinstance(command, app_commands.Command)
    assert command.description == "WIN5 시즌 생성·활성화·종료·취소·정보 수정을 관리합니다."

    asyncio.run(command.callback(group, interaction))  # type: ignore[arg-type,union-attr]

    assert authorization.calls == [(interaction, "win5.staff.season")]
    content, options = interaction.response.messages[0]
    assert content == "관리할 WIN5 시즌 작업을 선택해 주세요."
    assert options["ephemeral"] is True
    select = options["view"].children[0]
    assert [(option.value, option.label) for option in select.options] == [
        ("create", "시즌 생성"),
        ("activate", "시즌 활성화"),
        ("close", "시즌 종료"),
        ("cancel", "draft 시즌 취소"),
        ("edit", "시즌 정보 수정"),
    ]


def test_season_target_selector_uses_fresh_worker_query_and_bound_authorization() -> None:
    target = _season_snapshot(rounds=Win5SeasonRoundState(setup=2))
    season_queries = RecordingSeasonQueries(target=target)
    authorization = RecordingAuthorization()
    adapter = _adapter(
        RecordingQueries(),
        season_queries=season_queries,
        authorization=authorization,
    )
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5StaffSeasonActionView(adapter=adapter, context=context)
    select = source_view.children[0]
    select._values = [Win5SeasonAction.ACTIVATE.value]
    event_loop_thread_id = get_ident()
    stop_calls, source_stopped_at_edit = _track_stop_before_edit(source_view, interaction)

    asyncio.run(select.callback(interaction))  # type: ignore[attr-defined,arg-type]

    assert source_stopped_at_edit == [True]
    assert stop_calls == [True]
    assert authorization.calls == [(interaction, "win5.staff.season")]
    assert season_queries.list_calls[0][:3] == (Win5SeasonAction.ACTIVATE, 0, 25)
    assert season_queries.list_calls[0][3] != event_loop_thread_id
    edit = interaction.response.edits[0]
    assert isinstance(edit["view"], Win5SeasonTargetView)
    assert edit["view"].children[0].options[0].label == "2026 @\u200beveryone 하반기"


def test_season_create_modal_builds_zero_write_preview_then_confirm_uses_interaction_key() -> None:
    season_commands = RecordingSeasonCommands(result=_changed_season())
    authorization = RecordingAuthorization()
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        season_commands=season_commands,
        authorization=authorization,
        preparation=preparation,
    )
    opener = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(opener)
    source_view = Win5StaffSeasonActionView(adapter=adapter, context=context)
    select = source_view.children[0]
    select._values = [Win5SeasonAction.CREATE.value]

    asyncio.run(select.callback(opener))  # type: ignore[attr-defined,arg-type]
    modal = opener.response.modals[0]
    assert isinstance(modal, Win5SeasonMetadataModal)
    modal.name._value = "2026 @everyone 하반기"
    modal.starts_at._value = "2026-08-30 15:30"
    modal.ends_at._value = ""
    modal.reason._value = "운영 확인"
    submit = RecordingInteraction()
    stop_calls, source_stopped_at_edit = _track_stop_before_edit(
        source_view,
        submit,
        deferred=True,
    )

    asyncio.run(modal.on_submit(submit))  # type: ignore[arg-type]

    assert source_stopped_at_edit == [True]
    assert stop_calls == [True]
    assert preparation.calls == [(submit, "win5.staff.season", True)]
    assert season_commands.create_calls == []
    preview_edit = submit.edits[0]
    assert "2026 @\u200beveryone 하반기" in preview_edit["content"]
    confirm_view = preview_edit["view"]
    assert isinstance(confirm_view, Win5SeasonConfirmView)
    assert isinstance(confirm_view._preview, Win5SeasonMutationPreview)

    confirm = RecordingInteraction(interaction_id=777)
    confirm_stop_calls = _track_stop(confirm_view)
    asyncio.run(
        adapter.confirm_season_mutation(
            confirm,
            context=context,
            preview=confirm_view._preview,
            source_view=confirm_view,
        )
    )

    command, worker_id = season_commands.create_calls[0]
    assert command.name == "2026 @everyone 하반기"
    assert command.starts_at == datetime(2026, 8, 30, 6, 30, tzinfo=UTC)
    assert command.idempotency_key == "win5-season-create:777"
    assert worker_id != get_ident()
    assert "WIN5 시즌 작업을 완료했습니다" in confirm.edits[0]["content"]
    assert confirm_stop_calls == [True]

    duplicate = RecordingInteraction(interaction_id=778)
    asyncio.run(
        adapter.confirm_season_mutation(
            duplicate,
            context=context,
            preview=confirm_view._preview,
            source_view=confirm_view,
        )
    )

    assert len(season_commands.create_calls) == 1
    assert "이미 처리 중이거나 완료" in duplicate.response.messages[0][0]


def test_season_transition_preview_requeries_current_target_without_writing() -> None:
    target = _season_snapshot(rounds=Win5SeasonRoundState(setup=2))
    season_queries = RecordingSeasonQueries(target=target)
    season_commands = RecordingSeasonCommands(result=_changed_season())
    preparation = RecordingPreparation()
    adapter = _adapter(
        RecordingQueries(),
        season_queries=season_queries,
        season_commands=season_commands,
        preparation=preparation,
    )
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5SeasonTargetView(
        adapter=adapter,
        context=context,
        page=Win5SeasonTargetPage(
            action=Win5SeasonAction.ACTIVATE,
            offset=0,
            limit=25,
            choices=(target,),
        ),
    )
    select = source_view.children[0]
    select._values = [str(target.id)]
    stop_calls, source_stopped_at_edit = _track_stop_before_edit(
        source_view,
        interaction,
        deferred=True,
    )

    asyncio.run(select.callback(interaction))  # type: ignore[attr-defined,arg-type]

    assert source_stopped_at_edit == [True]
    assert stop_calls == [True]
    assert preparation.calls == [(interaction, "win5.staff.season", True)]
    assert season_queries.get_calls[0][:2] == (7, Win5SeasonAction.ACTIVATE)
    assert season_commands.transition_calls == []
    assert "draft → active" in interaction.edits[0]["content"]
    assert "setup 2" in interaction.edits[0]["content"]
    assert isinstance(interaction.edits[0]["view"], Win5SeasonConfirmView)


def test_season_target_replacement_failure_stops_source_and_prompts_reopen() -> None:
    target = _season_snapshot(rounds=Win5SeasonRoundState(setup=2))
    adapter = _adapter(
        RecordingQueries(),
        season_queries=RecordingSeasonQueries(target=target),
    )
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5StaffSeasonActionView(adapter=adapter, context=context)
    stop_calls, source_stopped_at_edit = _track_stop_before_edit(
        source_view,
        interaction,
        fail=True,
    )

    asyncio.run(
        adapter.show_season_targets(
            interaction,
            context=context,
            action=Win5SeasonAction.ACTIVATE,
            offset=0,
            source_view=source_view,
        )
    )

    assert source_stopped_at_edit == [True]
    assert stop_calls == [True]
    assert interaction.response.messages[-1][0] == (
        "WIN5 Season 화면을 갱신하지 못했습니다. `/win5 staff season`을 다시 열어 주세요."
    )


def test_season_deferred_preview_failure_stops_source_and_uses_followup() -> None:
    target = _season_snapshot(rounds=Win5SeasonRoundState(setup=2))
    adapter = _adapter(
        RecordingQueries(),
        season_queries=RecordingSeasonQueries(target=target),
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
        adapter.preview_season_transition(
            interaction,
            context=context,
            action=Win5SeasonAction.ACTIVATE,
            season_id=7,
            source_view=source_view,
        )
    )

    assert source_stopped_at_edit == [True]
    assert stop_calls == [True]
    assert interaction.followup.messages[-1][0] == (
        "WIN5 Season 화면을 갱신하지 못했습니다. `/win5 staff season`을 다시 열어 주세요."
    )


def test_season_final_delivery_failure_still_closes_source_after_command() -> None:
    season_commands = RecordingSeasonCommands(result=_changed_season())
    adapter = _adapter(RecordingQueries(), season_commands=season_commands)
    opening = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(opening)
    source_view = Win5SeasonConfirmView(
        adapter=adapter,
        context=context,
        preview=Win5SeasonMutationPreview(
            action=Win5SeasonAction.CREATE,
            desired_name="2026 하반기",
        ),
    )
    stop_calls = _track_stop(source_view)
    interaction = RecordingInteraction(interaction_id=780)

    async def fail_delivery(**_kwargs: object) -> None:
        raise RuntimeError("Discord terminal edit failed")

    interaction.edit_original_response = fail_delivery  # type: ignore[method-assign]

    asyncio.run(
        adapter.confirm_season_mutation(
            interaction,
            context=context,
            preview=source_view._preview,
            source_view=source_view,
        )
    )

    assert len(season_commands.create_calls) == 1
    assert stop_calls == [True]


def test_season_cancel_delivery_failure_still_closes_source_without_write() -> None:
    season_commands = RecordingSeasonCommands(result=_changed_season())
    adapter = _adapter(RecordingQueries(), season_commands=season_commands)
    interaction = RecordingInteraction()
    context = Win5StaffInteractionContext.from_interaction(interaction)
    source_view = Win5SeasonConfirmView(
        adapter=adapter,
        context=context,
        preview=Win5SeasonMutationPreview(
            action=Win5SeasonAction.CREATE,
            desired_name="2026 하반기",
        ),
    )
    stop_calls, source_stopped_at_edit = _track_stop_before_edit(
        source_view,
        interaction,
        fail=True,
    )

    asyncio.run(
        adapter.cancel_season_mutation(
            interaction,
            context=context,
            source_view=source_view,
        )
    )

    assert season_commands.create_calls == []
    assert source_stopped_at_edit == [True]
    assert stop_calls == [True]
