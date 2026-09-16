"""Discord adapter tests for WIN5 member submission history."""

from __future__ import annotations

from win5_member_discord_test_support import (
    UTC,
    RecordingInteraction,
    RecordingInteractionAuthorization,
    RecordingPreparation,
    RecordingQueries,
    Win5JudgementOutcome,
    Win5MemberCommandGroup,
    Win5MemberInteractionContext,
    Win5MemberQueryIdentityError,
    Win5MemberResult,
    Win5MemberSubmission,
    Win5MemberSubmissionPick,
    Win5NormalJudgementItem,
    Win5RaceCard,
    Win5RaceEntryOption,
    Win5RoundStatus,
    Win5SpecialJudgementItem,
    Win5SpecialSubmissionJudgement,
    Win5SubmissionsInvalidSourceError,
    Win5SubmissionsUnavailableError,
    Win5SubmissionsView,
    Win5SubmissionTier,
    _adapter,
    _submissions_fixture,
    _void_submission_history_fixture,
    app_commands,
    asyncio,
    datetime,
    discord,
    format_submission_history_round,
    get_ident,
    pytest,
    replace,
)


def test_submissions_registration_opens_private_snapshot_view_through_worker() -> None:
    dashboard, pages = _submissions_fixture()
    queries = RecordingQueries(dashboard=dashboard, submission_history_pages=pages)
    preparation = RecordingPreparation()
    group = Win5MemberCommandGroup(adapter=_adapter(queries, preparation))
    command = group.get_command("submissions")
    interaction = RecordingInteraction()
    event_loop_thread_id = get_ident()

    assert isinstance(command, app_commands.Command)
    assert command.description == "현재 시즌의 WIN5 제출과 채점 결과를 조회합니다."
    assert command.parameters == []

    asyncio.run(command.callback(group, interaction))  # type: ignore[arg-type,union-attr]

    assert preparation.calls == [(interaction, "win5.submissions", True)]
    assert queries.dashboard_calls[0][0] == "123"
    assert queries.dashboard_calls[0][1] != event_loop_thread_id
    assert queries.submission_history_calls[0][0:6] == ("123", 11, 0, 5, 0, 5)
    assert queries.submission_history_calls[0][6] != event_loop_thread_id
    assert len(interaction.edits) == 1
    view = interaction.edits[0]["view"]
    assert isinstance(view, Win5SubmissionsView)
    assert interaction.edits[0]["content"] == (
        "## WIN5 제출 이력\n"
        "시즌: 2026 @\u200beveryone 하반기\n"
        "라운드: Open Round @\u200beveryone\n"
        "유형/상태: Normal / 진행 중\n"
        "제출 이력: 0건 · 페이지 1/1\n"
        "내 제출: 없음"
    )
    assert len(view.children) == 5
    assert interaction.followup.messages == []


def test_submissions_initial_root_detail_status_race_uses_fixed_message() -> None:
    dashboard, pages = _submissions_fixture()
    raced_detail = replace(
        pages[0],
        round=replace(pages[0].round, status=Win5RoundStatus.SCORED),
    )
    interaction = RecordingInteraction()

    asyncio.run(
        _adapter(
            RecordingQueries(
                dashboard=dashboard,
                submission_history_pages=(raced_detail,),
            ),
            RecordingPreparation(),
        ).show_submissions(interaction)  # type: ignore[arg-type]
    )

    assert interaction.edits[0]["content"] == (
        "제출 이력을 조회하지 못했습니다: 라운드 상태가 바뀌었습니다. 다시 실행해 주세요."
    )
    assert "view" not in interaction.edits[0]


def test_submissions_formatter_preserves_history_and_event_backed_normal_judgement() -> None:
    _dashboard, pages = _submissions_fixture()

    message = format_submission_history_round(pages[1])

    assert "@everyone" not in message
    assert "제출 1: TOP1 / 취소 이력 / version 2" in message
    assert "제출 2: TOP3 / 제출 중 / version 4" in message
    assert "1착 정확(+3)" in message
    assert "2착 TOP5 밖(0)" in message
    assert "3착 미입력(MISS)" in message
    assert "정확 1 / 순위 다름 0 / TOP5 밖 1 / MISS 1" in message
    assert "Season +3 / TOP1 +0 / Circle Point +10" in message
    assert "확정 결과: 1착 Gate 1" in message

    special_message = format_submission_history_round(pages[2])
    assert "Kyoto 10R" in special_message
    assert "제출=결과 Gate 8 — Do Deuce" in special_message
    assert "정확(+1)" in special_message
    assert "정확 1 / 불일치 0 / MISS 0" in special_message
    assert "Season/TOP1 +1/+1 · Circle Point +0" in special_message
    assert "@everyone" not in special_message


def test_submissions_formatter_distinguishes_mixed_void_and_all_void_cancelled_history() -> None:
    _dashboard, mixed_page, cancelled_page = _void_submission_history_fixture()

    mixed_message = format_submission_history_round(mixed_page)
    cancelled_message = format_submission_history_round(cancelled_page)

    assert "Mixed Void Round" in mixed_message
    assert "제출 Gate 9 / 결과 없음 / 사유 강풍으로 공식 취소 @\u200beveryone" in mixed_message
    assert "공식 취소(0)" in mixed_message
    assert "VOID 1" in mixed_message
    assert "All Void Round" in cancelled_message
    assert "유형/상태: Special / 취소됨" in cancelled_message
    assert "Kyoto 10R @\u200beveryone: 폭우로 공식 취소" in cancelled_message
    assert "Tokyo 취소 Race @\u200beveryone: 강풍으로 공식 취소 @\u200beveryone" in cancelled_message
    assert "Kyoto 10R @\u200beveryone: Gate 8 — Do Deuce @\u200beveryone" in cancelled_message
    assert "Tokyo 취소 Race @\u200beveryone: Gate 9" in cancelled_message
    assert "  합계:" not in cancelled_message
    assert "@everyone" not in mixed_message
    assert "@everyone" not in cancelled_message


def test_submissions_cancelled_mode_reauthorizes_and_loads_fresh_all_void_detail() -> None:
    dashboard, mixed_page, cancelled_page = _void_submission_history_fixture()
    _base_dashboard, base_pages = _submissions_fixture()
    authorization = RecordingInteractionAuthorization()
    queries = RecordingQueries(
        dashboard=dashboard,
        submission_history_pages=(*base_pages, mixed_page, cancelled_page),
    )
    opener = RecordingInteraction()
    view = Win5SubmissionsView(
        adapter=_adapter(
            queries,
            RecordingPreparation(),
            interaction_authorization=authorization,
        ),
        context=Win5MemberInteractionContext.from_interaction(opener),
        dashboard=dashboard,
        detail=base_pages[0],
    )
    cancelled = next(item for item in view.children if isinstance(item, discord.ui.Button) and item.label == "취소됨")
    interaction = RecordingInteraction()

    asyncio.run(cancelled.callback(interaction))

    assert authorization.calls == [(interaction, "win5.submissions")]
    assert queries.dashboard_calls[-1][0] == "123"
    assert queries.submission_history_calls[-1][0:6] == ("123", 15, 0, 5, 0, 5)
    replacement = interaction.edits[0]["view"]
    assert isinstance(replacement, Win5SubmissionsView)
    assert view.mode == "open"
    assert replacement.mode == "cancelled"
    assert replacement.detail == cancelled_page
    assert "All Void Round" in interaction.edits[0]["content"]
    replacement_cancelled = next(
        item for item in replacement.children if isinstance(item, discord.ui.Button) and item.label == "취소됨"
    )
    assert replacement_cancelled.style == discord.ButtonStyle.primary


def test_submissions_normal_five_row_page_never_omits_detail_lines() -> None:
    _dashboard, pages = _submissions_fixture()
    source_page = pages[1]
    source_round = source_page.round
    source_submission = source_round.submissions[1]
    entries = tuple(replace(entry, name="Long Horse Name " * 10) for entry in source_round.races[0].entries)
    results = tuple(replace(result, reference_entry=entries[result.position - 1]) for result in source_round.results)
    submissions: list[Win5MemberSubmission] = []
    for submission_index in range(5):
        picks = tuple(
            Win5MemberSubmissionPick(
                id=10_000 + submission_index * 10 + position,
                race_id=source_round.races[0].id,
                position=position,
                race_entry_id=entries[position - 1].id,
                gate_number=None,
                reference_entry=entries[position - 1],
            )
            for position in range(1, 6)
        )
        score_items = tuple(
            Win5NormalJudgementItem(
                position=position,
                submission_pick_id=picks[position - 1].id,
                matched_result_id=results[position - 1].id,
                outcome=Win5JudgementOutcome.EXACT,
                season_score_delta=3,
            )
            for position in range(1, 6)
        )
        score = replace(
            source_submission.judgement.score,  # type: ignore[union-attr]
            tier=Win5SubmissionTier.TOP5,
            items=score_items,
            exact_count=5,
            wrong_position_count=0,
            off_board_count=0,
            missing_count=0,
            season_score_delta=15,
            circle_point_reward=100,
        )
        judgement = replace(
            source_submission.judgement,  # type: ignore[arg-type]
            event_id=9_100 + submission_index,
            submission_id=700 + submission_index,
            tier=Win5SubmissionTier.TOP5,
            score=score,
        )
        submissions.append(
            replace(
                source_submission,
                id=700 + submission_index,
                tier=Win5SubmissionTier.TOP5,
                picks=picks,
                judgement=judgement,
            )
        )
    page = replace(
        source_page,
        round=replace(
            source_round,
            races=(replace(source_round.races[0], entries=entries),),
            results=results,
            submissions=tuple(submissions),
        ),
        total_submission_count=5,
    )

    message = format_submission_history_round(page)

    assert "항목 생략" not in message
    assert "제출 5: TOP5" in message
    assert len(message) < 3500


def test_submissions_special_five_by_five_page_never_omits_detail_lines() -> None:
    _dashboard, pages = _submissions_fixture()
    source_page = pages[2]
    races = tuple(
        Win5RaceCard(
            id=1_300 + race_index,
            name="Long Special Race Name " * 10,
            scheduled_at=None,
            entries=(
                Win5RaceEntryOption(
                    id=4_000 + race_index,
                    gate_number=race_index,
                    name="Long Reference Horse Name " * 10,
                ),
            ),
        )
        for race_index in range(1, 6)
    )
    results = tuple(
        Win5MemberResult(
            id=30_000 + race_index,
            race_id=race.id,
            position=1,
            race_entry_id=None,
            gate_number=race.entries[0].gate_number,
            reference_entry=race.entries[0],
        )
        for race_index, race in enumerate(races, start=1)
    )
    source_judgement = source_page.round.submissions[0].judgement
    assert isinstance(source_judgement, Win5SpecialSubmissionJudgement)
    submissions = tuple(
        replace(
            source_page.round.submissions[0],
            id=800 + submission_index,
            picks=tuple(
                Win5MemberSubmissionPick(
                    id=20_000 + submission_index * 10 + race_index,
                    race_id=race.id,
                    position=1,
                    race_entry_id=None,
                    gate_number=race.entries[0].gate_number,
                    reference_entry=race.entries[0],
                )
                for race_index, race in enumerate(races, start=1)
            ),
            judgement=replace(
                source_judgement,
                event_id=40_000 + submission_index,
                submission_id=800 + submission_index,
                race_count=5,
                exact_count=5,
                season_score_delta=5,
                top1_score_delta=5,
                items=tuple(
                    Win5SpecialJudgementItem(
                        race_id=race.id,
                        submission_pick_id=20_000 + submission_index * 10 + race_index,
                        matched_result_id=results[race_index - 1].id,
                        outcome=Win5JudgementOutcome.EXACT,
                        season_score_delta=1,
                    )
                    for race_index, race in enumerate(races, start=1)
                ),
            ),
        )
        for submission_index in range(5)
    )
    page = replace(
        source_page,
        season_name="Long Season Name " * 10,
        round=replace(
            source_page.round,
            name="Long Round Name " * 10,
            races=races,
            results=results,
            submissions=submissions,
        ),
        total_submission_count=5,
        total_race_count=5,
    )

    message = format_submission_history_round(page)

    assert "항목 생략" not in message
    assert "제출 5: Special" in message
    assert len(message) < 3500


def test_submissions_cancelled_five_by_five_page_never_omits_void_reasons() -> None:
    _dashboard, _mixed_page, source_page = _void_submission_history_fixture()
    races = tuple(
        Win5RaceCard(
            id=1_500 + race_index,
            name="Long Cancelled Special Race Name " * 10,
            scheduled_at=None,
            entries=(
                Win5RaceEntryOption(
                    id=5_000 + race_index,
                    gate_number=race_index,
                    name="Long Cancelled Reference Name " * 10,
                ),
            ),
            void_reason="Long official cancellation reason " * 10,
            voided_at=datetime(2026, 8, 25, 1, race_index, tzinfo=UTC),
        )
        for race_index in range(1, 6)
    )
    submissions = tuple(
        replace(
            source_page.round.submissions[0],
            id=900 + submission_index,
            picks=tuple(
                Win5MemberSubmissionPick(
                    id=30_000 + submission_index * 10 + race_index,
                    race_id=race.id,
                    position=1,
                    race_entry_id=None,
                    gate_number=race.entries[0].gate_number,
                    reference_entry=race.entries[0],
                )
                for race_index, race in enumerate(races, start=1)
            ),
        )
        for submission_index in range(5)
    )
    page = replace(
        source_page,
        round=replace(
            source_page.round,
            races=races,
            submissions=submissions,
        ),
        total_submission_count=5,
        total_race_count=5,
        total_void_race_count=5,
    )

    message = format_submission_history_round(page)

    assert "항목 생략" not in message
    assert "제출 5: Special" in message
    assert "Long official cancellation" in message
    assert len(message) < 3500


def test_submissions_components_reauthorize_context_and_fresh_query_each_selection() -> None:
    dashboard, pages = _submissions_fixture()
    authorization = RecordingInteractionAuthorization()
    queries = RecordingQueries(dashboard=dashboard, submission_history_pages=pages)
    adapter = _adapter(
        queries,
        RecordingPreparation(),
        interaction_authorization=authorization,
    )
    opener = RecordingInteraction()
    view = Win5SubmissionsView(
        adapter=adapter,
        context=Win5MemberInteractionContext.from_interaction(opener),
        dashboard=dashboard,
        detail=pages[0],
    )
    scored = next(item for item in view.children if isinstance(item, discord.ui.Button) and item.label == "채점 완료")

    mismatched = RecordingInteraction(user_id=999)
    asyncio.run(scored.callback(mismatched))

    assert authorization.calls == []
    assert mismatched.response.messages[0][1]["ephemeral"] is True
    assert view.mode == "open"

    switch_interaction = RecordingInteraction()
    asyncio.run(scored.callback(switch_interaction))

    assert authorization.calls == [(switch_interaction, "win5.submissions")]
    assert queries.submission_history_calls[0][0:6] == ("123", 12, 0, 5, 0, 5)
    assert switch_interaction.response.defers == [{}]
    scored_view = switch_interaction.edits[0]["view"]
    assert isinstance(scored_view, Win5SubmissionsView)
    assert view.mode == "open"
    assert scored_view.mode == "scored"
    assert "Scored Round" in switch_interaction.edits[0]["content"]

    select_interaction = RecordingInteraction()
    asyncio.run(scored_view.select_round(select_interaction, round_id=13))

    assert authorization.calls[-1] == (select_interaction, "win5.submissions")
    assert queries.submission_history_calls[1][0:6] == ("123", 13, 0, 5, 0, 5)
    assert select_interaction.response.defers == [{}]
    assert "Special Scored Round" in select_interaction.edits[0]["content"]
    assert "Kyoto 10R" in select_interaction.edits[0]["content"]
    assert "정확(+1)" in select_interaction.edits[0]["content"]


def test_submissions_replacement_stops_source_and_recovers_delivery_failure() -> None:
    dashboard, pages = _submissions_fixture()
    view = Win5SubmissionsView(
        adapter=_adapter(
            RecordingQueries(dashboard=dashboard, submission_history_pages=pages),
            RecordingPreparation(),
            interaction_authorization=RecordingInteractionAuthorization(),
        ),
        context=Win5MemberInteractionContext.from_interaction(RecordingInteraction()),
        dashboard=dashboard,
        detail=pages[0],
    )
    original_stop = view.stop
    stop_calls: list[bool] = []

    def record_stop() -> None:
        stop_calls.append(True)
        original_stop()

    view.stop = record_stop  # type: ignore[method-assign]
    scored = next(item for item in view.children if isinstance(item, discord.ui.Button) and item.label == "채점 완료")
    interaction = RecordingInteraction(edit_fails=True)

    asyncio.run(scored.callback(interaction))

    assert stop_calls == [True]
    assert view.mode == "open"
    assert view.detail == pages[0]
    assert interaction.edits == []
    assert "화면을 갱신하지 못했습니다" in interaction.followup.messages[0][0]

    duplicate = RecordingInteraction()
    asyncio.run(scored.callback(duplicate))

    assert duplicate.edits == []
    assert "이미 시작" in duplicate.followup.messages[0][0]


def test_submissions_stale_open_selection_refreshes_root_without_showing_scored_detail() -> None:
    dashboard, pages = _submissions_fixture()
    new_open_summary = replace(
        dashboard.open_rounds[0],
        id=14,
        name="New Open Round",
    )
    moved_scored_summary = replace(
        dashboard.open_rounds[0],
        status=Win5RoundStatus.SCORED,
        submission_count=1,
    )
    refreshed_dashboard = replace(
        dashboard,
        open_rounds=(new_open_summary,),
        scored_rounds=(*dashboard.scored_rounds, moved_scored_summary),
    )
    new_open_page = replace(
        pages[0],
        round=replace(pages[0].round, id=14, name="New Open Round"),
    )
    queries = RecordingQueries(
        dashboard=refreshed_dashboard,
        submission_history_pages=(new_open_page,),
    )
    view = Win5SubmissionsView(
        adapter=_adapter(
            queries,
            RecordingPreparation(),
            interaction_authorization=RecordingInteractionAuthorization(),
        ),
        context=Win5MemberInteractionContext.from_interaction(RecordingInteraction()),
        dashboard=dashboard,
        detail=pages[0],
    )
    interaction = RecordingInteraction()

    asyncio.run(view.select_round(interaction, round_id=11))

    assert queries.dashboard_calls[-1][0] == "123"
    assert queries.submission_history_calls[-1][0:6] == ("123", 14, 0, 5, 0, 5)
    replacement = interaction.edits[0]["view"]
    assert isinstance(replacement, Win5SubmissionsView)
    assert view.mode == "open"
    assert view.selected_round_id == 11
    assert replacement.mode == "open"
    assert replacement.selected_round_id == 14
    assert "New Open Round" in interaction.edits[0]["content"]
    assert "Scored Round" not in interaction.edits[0]["content"]
    assert "상태가 바뀌어 현재 목록" in interaction.followup.messages[0][0]


def test_submissions_rejects_status_change_between_root_and_detail_queries() -> None:
    dashboard, pages = _submissions_fixture()
    raced_detail = replace(
        pages[0],
        round=replace(pages[0].round, status=Win5RoundStatus.SCORED),
    )
    queries = RecordingQueries(
        dashboard=dashboard,
        submission_history_pages=(raced_detail,),
    )
    view = Win5SubmissionsView(
        adapter=_adapter(
            queries,
            RecordingPreparation(),
            interaction_authorization=RecordingInteractionAuthorization(),
        ),
        context=Win5MemberInteractionContext.from_interaction(RecordingInteraction()),
        dashboard=dashboard,
        detail=pages[0],
    )
    interaction = RecordingInteraction()

    asyncio.run(view.select_round(interaction, round_id=11))

    assert view.detail is pages[0]
    assert interaction.edits == []
    assert "라운드 상태가 바뀌었습니다" in interaction.followup.messages[0][0]


def test_submissions_scored_selector_honors_25_options_and_disambiguates_titles() -> None:
    dashboard, pages = _submissions_fixture()
    source = dashboard.scored_rounds[1]
    scored_rounds = tuple(replace(source, id=100 + index, name="동명 라운드") for index in range(25))
    bounded = replace(dashboard, scored_rounds=scored_rounds)
    detail = replace(
        pages[2],
        round=replace(pages[2].round, id=100, name="동명 라운드"),
    )
    view = Win5SubmissionsView(
        adapter=_adapter(RecordingQueries(), RecordingPreparation()),
        context=Win5MemberInteractionContext.from_interaction(RecordingInteraction()),
        dashboard=bounded,
        detail=pages[0],
    )
    view._activate("scored", detail=detail)  # noqa: SLF001
    select = next(item for item in view.children if isinstance(item, discord.ui.Select))

    assert len(select.options) == 25
    assert select.options[0].label == "동명 라운드 · ID 100"
    assert select.options[-1].label == "동명 라운드 · ID 124"
    assert sum(option.default for option in select.options) == 1


def test_submissions_open_selector_accepts_all_25_concurrent_rounds() -> None:
    dashboard, pages = _submissions_fixture()
    source = dashboard.open_rounds[0]
    open_rounds = tuple(replace(source, id=200 + index, name=f"Open Round {index}") for index in range(25))
    bounded = replace(dashboard, open_rounds=open_rounds)
    detail = replace(
        pages[0],
        round=replace(pages[0].round, id=200, name="Open Round 0"),
    )

    view = Win5SubmissionsView(
        adapter=_adapter(RecordingQueries(), RecordingPreparation()),
        context=Win5MemberInteractionContext.from_interaction(RecordingInteraction()),
        dashboard=bounded,
        detail=detail,
    )
    select = next(item for item in view.children if isinstance(item, discord.ui.Select))

    assert len(select.options) == 25
    assert [option.value for option in select.options] == [str(round_.id) for round_ in open_rounds]


def test_submissions_history_pages_five_rows_without_silent_truncation() -> None:
    dashboard, pages = _submissions_fixture()
    source_submission = pages[1].round.submissions[0]
    first_submissions = tuple(replace(source_submission, id=500 + index) for index in range(5))
    last_submission = replace(source_submission, id=505)
    first_page = replace(
        pages[1],
        round=replace(pages[1].round, submissions=first_submissions),
        total_submission_count=6,
    )
    last_page = replace(
        first_page,
        round=replace(first_page.round, submissions=(last_submission,)),
        submission_offset=5,
    )
    dashboard = replace(
        dashboard,
        scored_rounds=(
            replace(dashboard.scored_rounds[0], submission_count=6),
            dashboard.scored_rounds[1],
        ),
    )
    authorization = RecordingInteractionAuthorization()
    queries = RecordingQueries(
        dashboard=dashboard,
        submission_history_pages=(pages[0], first_page, last_page, pages[2]),
    )
    view = Win5SubmissionsView(
        adapter=_adapter(
            queries,
            RecordingPreparation(),
            interaction_authorization=authorization,
        ),
        context=Win5MemberInteractionContext.from_interaction(RecordingInteraction()),
        dashboard=dashboard,
        detail=pages[0],
    )

    switch_interaction = RecordingInteraction()
    asyncio.run(view.scored_button.callback(switch_interaction))
    scored_view = switch_interaction.edits[0]["view"]
    assert isinstance(scored_view, Win5SubmissionsView)
    next_button = next(
        item for item in scored_view.children if isinstance(item, discord.ui.Button) and item.label == "다음 제출"
    )
    interaction = RecordingInteraction()
    asyncio.run(next_button.callback(interaction))

    assert queries.submission_history_calls[-1][0:6] == ("123", 12, 5, 5, 0, 5)
    assert interaction.response.defers == [{}]
    assert "제출 이력: 6건 · 페이지 2/2" in interaction.edits[0]["content"]
    assert "제출 6: TOP1 / 취소 이력 / version 2" in interaction.edits[0]["content"]
    assert "제출 5:" not in interaction.edits[0]["content"]
    assert len(interaction.edits[0]["content"]) < 3500


def test_submissions_refresh_discovers_new_replacement_history_page() -> None:
    dashboard, pages = _submissions_fixture()
    source_submission = replace(
        pages[1].round.submissions[0],
        round_id=11,
        picks=(),
    )
    first_five = tuple(replace(source_submission, id=600 + index) for index in range(5))
    initial_page = replace(
        pages[0],
        round=replace(pages[0].round, submissions=first_five),
        total_submission_count=5,
    )
    refreshed_page = replace(initial_page, total_submission_count=6)
    refreshed_dashboard = replace(
        dashboard,
        open_rounds=(replace(dashboard.open_rounds[0], submission_count=6),),
    )
    queries = RecordingQueries(
        dashboard=refreshed_dashboard,
        submission_history_pages=(refreshed_page,),
    )
    view = Win5SubmissionsView(
        adapter=_adapter(
            queries,
            RecordingPreparation(),
            interaction_authorization=RecordingInteractionAuthorization(),
        ),
        context=Win5MemberInteractionContext.from_interaction(RecordingInteraction()),
        dashboard=dashboard,
        detail=initial_page,
    )
    assert not any(isinstance(item, discord.ui.Button) and item.label == "다음 제출" for item in view.children)
    refresh = next(item for item in view.children if isinstance(item, discord.ui.Button) and item.label == "새로고침")
    interaction = RecordingInteraction()

    asyncio.run(refresh.callback(interaction))

    assert queries.dashboard_calls[-1][0] == "123"
    assert queries.submission_history_calls[-1][0:6] == ("123", 11, 0, 5, 0, 5)
    assert "제출 이력: 6건 · 페이지 1/2" in interaction.edits[0]["content"]
    replacement = interaction.edits[0]["view"]
    assert isinstance(replacement, Win5SubmissionsView)
    next_button = next(
        item for item in replacement.children if isinstance(item, discord.ui.Button) and item.label == "다음 제출"
    )
    assert next_button.disabled is False


def test_submissions_special_races_page_independently_with_event_projection() -> None:
    dashboard, pages = _submissions_fixture()
    source_page = pages[2]
    source_submission = source_page.round.submissions[0]
    races = tuple(
        Win5RaceCard(
            id=1300 + index,
            name=f"Special Race {index} @everyone",
            scheduled_at=None,
            entries=(
                Win5RaceEntryOption(
                    id=4000 + index,
                    gate_number=index,
                    name=f"Reference {index} @everyone",
                ),
            ),
        )
        for index in range(1, 7)
    )
    picks = tuple(
        Win5MemberSubmissionPick(
            id=8000 + index,
            race_id=1300 + index,
            position=1,
            race_entry_id=None,
            gate_number=index,
            reference_entry=races[index - 1].entries[0],
        )
        for index in range(1, 7)
    )
    results = tuple(
        Win5MemberResult(
            id=9000 + index,
            race_id=1300 + index,
            position=1,
            race_entry_id=None,
            gate_number=index,
            reference_entry=races[index - 1].entries[0],
        )
        for index in range(1, 7)
    )
    judgements = tuple(
        Win5SpecialJudgementItem(
            race_id=1300 + index,
            submission_pick_id=8000 + index,
            matched_result_id=9000 + index,
            outcome=Win5JudgementOutcome.EXACT,
            season_score_delta=1,
        )
        for index in range(1, 7)
    )
    source_judgement = source_submission.judgement
    assert isinstance(source_judgement, Win5SpecialSubmissionJudgement)
    first_page = replace(
        source_page,
        round=replace(
            source_page.round,
            races=races[:5],
            results=results[:5],
            submissions=(
                replace(
                    source_submission,
                    picks=picks[:5],
                    judgement=replace(
                        source_judgement,
                        race_count=6,
                        exact_count=6,
                        season_score_delta=6,
                        top1_score_delta=6,
                        items=judgements[:5],
                    ),
                ),
            ),
        ),
        total_race_count=6,
    )
    last_page = replace(
        first_page,
        round=replace(
            first_page.round,
            races=races[5:],
            results=results[5:],
            submissions=(
                replace(
                    source_submission,
                    picks=picks[5:],
                    judgement=replace(
                        source_judgement,
                        race_count=6,
                        exact_count=6,
                        season_score_delta=6,
                        top1_score_delta=6,
                        items=judgements[5:],
                    ),
                ),
            ),
        ),
        race_offset=5,
    )
    queries = RecordingQueries(
        dashboard=dashboard,
        submission_history_pages=(last_page,),
    )
    view = Win5SubmissionsView(
        adapter=_adapter(
            queries,
            RecordingPreparation(),
            interaction_authorization=RecordingInteractionAuthorization(),
        ),
        context=Win5MemberInteractionContext.from_interaction(RecordingInteraction()),
        dashboard=dashboard,
        detail=pages[0],
    )
    view._activate("scored", detail=first_page)  # noqa: SLF001
    next_button = next(
        item for item in view.children if isinstance(item, discord.ui.Button) and item.label == "다음 Race"
    )
    interaction = RecordingInteraction()

    asyncio.run(next_button.callback(interaction))

    content = interaction.edits[0]["content"]
    assert queries.submission_history_calls[-1][0:6] == ("123", 13, 0, 5, 5, 5)
    assert interaction.response.defers == [{}]
    assert "Race: 6개 · 페이지 2/2" in content
    assert "Special Race 6" in content
    assert "Special Race 1" not in content
    assert "제출=결과 Gate 6 — Reference 6" in content
    assert "정확(+1)" in content
    assert "Season/TOP1 +6/+6 · Circle Point +0" in content


def test_submissions_page_query_failure_keeps_current_page() -> None:
    dashboard, pages = _submissions_fixture()
    first_page = replace(pages[1], total_submission_count=6)
    queries = RecordingQueries(
        dashboard=dashboard,
        submission_history_error=Win5SubmissionsUnavailableError("internal stale Round"),
    )
    view = Win5SubmissionsView(
        adapter=_adapter(
            queries,
            RecordingPreparation(),
            interaction_authorization=RecordingInteractionAuthorization(),
        ),
        context=Win5MemberInteractionContext.from_interaction(RecordingInteraction()),
        dashboard=dashboard,
        detail=pages[0],
    )
    view._activate("scored", detail=first_page)  # noqa: SLF001
    next_button = next(
        item for item in view.children if isinstance(item, discord.ui.Button) and item.label == "다음 제출"
    )
    interaction = RecordingInteraction()

    asyncio.run(next_button.callback(interaction))

    assert view.detail is first_page
    assert interaction.response.defers == [{}]
    assert interaction.followup.messages[0][1]["ephemeral"] is True
    assert "internal stale Round" not in interaction.followup.messages[0][0]


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (
            Win5MemberQueryIdentityError("internal identity"),
            "WIN5 이용에는 활성 Persona와 PID가 등록된 게임 계정이 필요합니다.",
        ),
        (Win5SubmissionsUnavailableError("internal season"), "현재 활성 WIN5 시즌이 없습니다."),
        (
            Win5SubmissionsInvalidSourceError("internal event"),
            "제출·채점 이력을 안전하게 표시할 수 없습니다. 운영진에게 알려 주세요.",
        ),
    ],
)
def test_submissions_expected_failure_uses_fixed_private_message(
    error: Exception,
    detail: str,
) -> None:
    queries = RecordingQueries(dashboard_error=error)
    interaction = RecordingInteraction()

    asyncio.run(_adapter(queries, RecordingPreparation()).show_submissions(interaction))  # type: ignore[arg-type]

    assert interaction.edits[0]["content"] == f"제출 이력을 조회하지 못했습니다: {detail}"
    assert "internal" not in interaction.edits[0]["content"]
