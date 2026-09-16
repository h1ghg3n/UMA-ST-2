"""Discord adapter tests for WIN5 member read surfaces."""

from __future__ import annotations

from win5_member_discord_test_support import (
    DatabaseRuntime,
    RecordingAutocompleteAuthorization,
    RecordingInteraction,
    RecordingInteractionAuthorization,
    RecordingPreparation,
    RecordingQueries,
    Win5ActiveSeasonInfo,
    Win5ActiveSeasonUnavailableError,
    Win5MemberCommandGroup,
    Win5MemberQueryIdentityError,
    Win5RoundType,
    Win5SeasonChoice,
    Win5SeasonStatus,
    Win5Standings,
    Win5StandingsSeasonUnavailableError,
    _adapter,
    _info,
    _round,
    _standings,
    app_commands,
    asyncio,
    compose_win5_member_command_group,
    create_engine,
    discord,
    format_active_season_info,
    format_open_rounds,
    format_standings,
    get_ident,
    logging,
    pytest,
)


def test_rounds_registration_and_public_query_use_worker_bridge() -> None:
    rounds = (
        _round(
            id_=11,
            round_type=Win5RoundType.NORMAL,
            name="제1회 우마 스테이크스",
            race_names=("우마 스테이크스",),
        ),
        _round(
            id_=12,
            round_type=Win5RoundType.SPECIAL,
            name="제2회 브리더스컵 데이",
            race_names=("Race A", "Race B"),
        ),
    )
    queries = RecordingQueries(rounds)
    preparation = RecordingPreparation()
    adapter = _adapter(queries, preparation)
    group = Win5MemberCommandGroup(adapter=adapter)
    command = group.get_command("rounds")
    interaction = RecordingInteraction()
    event_loop_thread_id = get_ident()

    assert group.name == "win5"
    assert group.description == "WIN5 예측과 순위를 관리합니다."
    assert [registered.name for registered in group.commands] == [
        "info",
        "rounds",
        "submit",
        "special-submit",
        "submissions",
        "cancel",
        "standings",
    ]
    assert isinstance(command, app_commands.Command)
    assert command.description == "현재 열린 WIN5 라운드를 조회합니다."
    assert command.parameters == []

    asyncio.run(command.callback(group, interaction))  # type: ignore[arg-type,union-attr]

    assert preparation.calls == [(interaction, "win5.rounds", False)]
    assert queries.calls[0][0] == 100
    assert queries.calls[0][1] != event_loop_thread_id
    assert interaction.edits[0]["content"] == (
        "현재 열린 WIN5 라운드\n- 제1회 우마 스테이크스\n- 제2회 브리더스컵 데이"
    )
    mentions = interaction.edits[0]["allowed_mentions"]
    assert isinstance(mentions, discord.AllowedMentions)
    assert mentions.everyone is False
    assert mentions.roles is False
    assert mentions.users is False
    assert interaction.followup.messages == []


def test_info_registration_and_private_query_use_persona_summary_and_kst() -> None:
    queries = RecordingQueries(info=_info())
    preparation = RecordingPreparation()
    group = Win5MemberCommandGroup(adapter=_adapter(queries, preparation))
    command = group.get_command("info")
    interaction = RecordingInteraction()
    event_loop_thread_id = get_ident()

    assert isinstance(command, app_commands.Command)
    assert command.description == "현재 활성 WIN5 시즌 정보를 조회합니다."
    assert command.parameters == []

    asyncio.run(command.callback(group, interaction))  # type: ignore[arg-type,union-attr]

    assert preparation.calls == [(interaction, "win5.info", True)]
    assert queries.info_calls[0][0] == "123"
    assert queries.info_calls[0][1] != event_loop_thread_id
    assert interaction.edits[0]["content"] == (
        "현재 WIN5 시즌 정보\n"
        "시즌: 2026 @\u200beveryone\n"
        "상태: 활성\n"
        "시작: 2026-08-25 09:00 KST\n"
        "종료: 2027-01-01 00:00 KST\n"
        "라운드: 전체 4개 / 오픈 2개\n"
        "내 시즌 승점: 21점\n"
        "내 TOP1 승점: 8점\n"
        "열린 라운드 확인: /win5 rounds"
    )
    mentions = interaction.edits[0]["allowed_mentions"]
    assert isinstance(mentions, discord.AllowedMentions)
    assert mentions.everyone is False
    assert interaction.delete_count == 0
    assert interaction.followup.messages == []


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (
            Win5MemberQueryIdentityError("internal identity detail"),
            "WIN5 이용에는 활성 Persona와 PID가 등록된 게임 계정이 필요합니다.",
        ),
        (Win5ActiveSeasonUnavailableError("internal Season detail"), "현재 활성 WIN5 시즌이 없습니다."),
    ],
)
def test_info_expected_failure_uses_fixed_private_actionable_message(error: Exception, detail: str) -> None:
    queries = RecordingQueries(info_error=error)
    interaction = RecordingInteraction()

    asyncio.run(_adapter(queries, RecordingPreparation()).show_active_season_info(interaction))  # type: ignore[arg-type]

    assert interaction.edits[0]["content"] == f"시즌 정보를 조회하지 못했습니다: {detail}"
    assert "internal" not in interaction.edits[0]["content"]
    assert interaction.delete_count == 0
    assert interaction.followup.messages == []


def test_info_internal_failure_edits_ephemeral_placeholder_without_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    queries = RecordingQueries(info_error=RuntimeError("password=mysql-secret"))
    interaction = RecordingInteraction()

    with caplog.at_level(logging.ERROR):
        asyncio.run(_adapter(queries, RecordingPreparation()).show_active_season_info(interaction))  # type: ignore[arg-type]

    content = interaction.edits[0]["content"]
    assert "참조 ID: `555`" in content
    assert "mysql-secret" not in content
    assert interaction.delete_count == 0
    assert interaction.followup.messages == []
    assert "Discord application call failed" in caplog.text
    assert "mysql-secret" not in caplog.text


def test_standings_registration_and_public_query_use_worker_bridge() -> None:
    queries = RecordingQueries(standings=_standings())
    preparation = RecordingPreparation()
    group = Win5MemberCommandGroup(adapter=_adapter(queries, preparation))
    command = group.get_command("standings")
    interaction = RecordingInteraction()
    event_loop_thread_id = get_ident()

    assert isinstance(command, app_commands.Command)
    assert command.description == "WIN5 시즌 순위를 조회합니다."
    parameters = {parameter.name: parameter for parameter in command.parameters}
    assert tuple(parameters) == ("season_id", "ranking")
    assert parameters["season_id"].required is True
    assert parameters["season_id"].autocomplete is True
    assert parameters["ranking"].required is False
    assert parameters["ranking"].default == "season"
    assert [(choice.name, choice.value) for choice in parameters["ranking"].choices] == [
        ("season", "season"),
        ("top1", "top1"),
    ]

    asyncio.run(command.callback(group, interaction, 7, "season"))  # type: ignore[arg-type,union-attr]

    assert preparation.calls == [(interaction, "win5.standings", False)]
    assert queries.standings_calls[0][:3] == (7, "season", 100)
    assert queries.standings_calls[0][3] != event_loop_thread_id
    content = interaction.edits[0]["content"]
    assert content == (
        "WIN5 시즌 종합 순위\n"
        "시즌: 2026 @\u200beveryone 하반기\n"
        "1위 우승자 @\u200beveryone / 30점\n"
        "2위 공동 2위 A / 20점\n"
        "2위 공동 2위 B / 20점"
    )
    assert "persona-" not in content
    mentions = interaction.edits[0]["allowed_mentions"]
    assert isinstance(mentions, discord.AllowedMentions)
    assert mentions.everyone is False
    assert interaction.followup.messages == []


def test_standings_autocomplete_authorizes_and_bounds_operator_titles() -> None:
    seasons = (
        Win5SeasonChoice(id=7, name="2026 @everyone 하반기", status=Win5SeasonStatus.ACTIVE),
        Win5SeasonChoice(id=8, name="2026 @everyone 하반기", status=Win5SeasonStatus.ACTIVE),
        Win5SeasonChoice(id=6, name="2026 @everyone 하반기", status=Win5SeasonStatus.CLOSED),
    )
    queries = RecordingQueries(season_choices=seasons)
    authorization = RecordingAutocompleteAuthorization()
    group = Win5MemberCommandGroup(adapter=_adapter(queries, RecordingPreparation(), authorization))
    interaction = RecordingInteraction()
    event_loop_thread_id = get_ident()

    choices = asyncio.run(group._standings_season_autocomplete(interaction, "하반기"))  # type: ignore[arg-type]

    assert authorization.calls == [(interaction, "win5.standings")]
    assert queries.season_search_calls[0][:2] == ("하반기", 25)
    assert queries.season_search_calls[0][2] != event_loop_thread_id
    assert [(choice.name, choice.value) for choice in choices] == [
        ("2026 @\u200beveryone 하반기 · 활성 · ID 7", 7),
        ("2026 @\u200beveryone 하반기 · 활성 · ID 8", 8),
        ("2026 @\u200beveryone 하반기 · 종료", 6),
    ]
    assert all(len(choice.name) <= 100 for choice in choices)
    assert interaction.edits == []


def test_standings_autocomplete_rejection_stops_before_query() -> None:
    queries = RecordingQueries()
    authorization = RecordingAutocompleteAuthorization(allowed=False)
    adapter = _adapter(queries, RecordingPreparation(), authorization)
    interaction = RecordingInteraction()

    choices = asyncio.run(adapter.autocomplete_standings_seasons(interaction, ""))  # type: ignore[arg-type]

    assert choices == []
    assert authorization.calls == [(interaction, "win5.standings")]
    assert queries.season_search_calls == []


def test_standings_autocomplete_failure_returns_empty_without_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    queries = RecordingQueries(standings_error=RuntimeError("password=mysql-secret"))
    adapter = _adapter(queries, RecordingPreparation())
    interaction = RecordingInteraction()

    with caplog.at_level(logging.ERROR):
        choices = asyncio.run(adapter.autocomplete_standings_seasons(interaction, ""))  # type: ignore[arg-type]

    assert choices == []
    assert "Discord autocomplete failed" in caplog.text
    assert "correlation_id=555" in caplog.text
    assert "mysql-secret" not in caplog.text


def test_standings_unavailable_season_replaces_public_placeholder_with_private_message() -> None:
    queries = RecordingQueries(standings_error=Win5StandingsSeasonUnavailableError("internal Season detail"))
    interaction = RecordingInteraction()

    asyncio.run(
        _adapter(queries, RecordingPreparation()).show_standings(
            interaction,  # type: ignore[arg-type]
            season_id=7,
            ranking="season",
        )
    )

    assert interaction.delete_count == 1
    assert interaction.edits == []
    assert len(interaction.followup.messages) == 1
    content, options = interaction.followup.messages[0]
    assert content == "순위를 조회하지 못했습니다: 선택한 시즌은 활성 또는 종료 상태여야 합니다."
    assert "internal" not in content
    assert options["ephemeral"] is True


def test_standings_internal_failure_uses_private_generic_error_without_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    queries = RecordingQueries(standings_error=RuntimeError("password=mysql-secret"))
    interaction = RecordingInteraction()

    with caplog.at_level(logging.ERROR):
        asyncio.run(
            _adapter(queries, RecordingPreparation()).show_standings(
                interaction,  # type: ignore[arg-type]
                season_id=7,
                ranking="top1",
            )
        )

    assert interaction.delete_count == 1
    content, options = interaction.followup.messages[0]
    assert "참조 ID: `555`" in content
    assert "mysql-secret" not in content
    assert options["ephemeral"] is True
    assert "mysql-secret" not in caplog.text


def test_standings_formatter_preserves_top1_empty_projection() -> None:
    message = format_standings(
        Win5Standings(
            season_id=7,
            season_name="2026 하반기",
            season_status=Win5SeasonStatus.CLOSED,
            ranking="top1",
        )
    )

    assert message == "WIN5 TOP1 순위\n시즌: 2026 하반기\n등록된 점수가 없습니다."


def test_composition_root_builds_group_with_injected_preparation_boundary() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    preparation = RecordingPreparation(allowed=False)
    authorization = RecordingAutocompleteAuthorization(allowed=False)
    interaction = RecordingInteraction()
    try:
        group = compose_win5_member_command_group(
            runtime,
            prepare_command=preparation,  # type: ignore[arg-type]
            authorize_autocomplete=authorization,  # type: ignore[arg-type]
            authorize_interaction=RecordingInteractionAuthorization(),  # type: ignore[arg-type]
        )
        command = group.get_command("rounds")

        asyncio.run(command.callback(group, interaction))  # type: ignore[arg-type,union-attr]
    finally:
        runtime.dispose()

    assert preparation.calls == [(interaction, "win5.rounds", False)]
    assert interaction.edits == []


def test_rounds_rejection_stops_before_application_query() -> None:
    queries = RecordingQueries()
    preparation = RecordingPreparation(allowed=False)
    interaction = RecordingInteraction()

    asyncio.run(_adapter(queries, preparation).list_open_rounds(interaction))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "win5.rounds", False)]
    assert queries.calls == []
    assert interaction.edits == []
    assert interaction.followup.messages == []


def test_rounds_application_failure_replaces_public_placeholder_with_private_generic_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    queries = RecordingQueries(error=RuntimeError("password=mysql-secret"))
    preparation = RecordingPreparation()
    interaction = RecordingInteraction()

    with caplog.at_level(logging.ERROR):
        asyncio.run(_adapter(queries, preparation).list_open_rounds(interaction))  # type: ignore[arg-type]

    assert interaction.delete_count == 1
    assert interaction.edits == []
    assert len(interaction.followup.messages) == 1
    content, options = interaction.followup.messages[0]
    assert "mysql-secret" not in content
    assert "참조 ID: `555`" in content
    assert options["ephemeral"] is True
    mentions = options["allowed_mentions"]
    assert isinstance(mentions, discord.AllowedMentions)
    assert mentions.everyone is False
    assert "Discord application call failed" in caplog.text
    assert "correlation_id=555" in caplog.text
    assert "mysql-secret" not in caplog.text


def test_round_formatter_escapes_untrusted_names_and_bounds_large_catalog() -> None:
    rounds = tuple(
        _round(
            id_=index,
            round_type=Win5RoundType.NORMAL,
            name=f"제{index}회 @everyone **Race {index}** {'x' * 80}",
            race_names=(f"Race {index}",),
        )
        for index in range(1, 101)
    )

    message = format_open_rounds(rounds)

    assert len(message) <= 1900
    assert "@everyone" not in message
    assert "\\*\\*Race 1\\*\\*" in message
    assert "항목 생략" in message


def test_round_formatter_preserves_empty_catalog_message() -> None:
    assert format_open_rounds(()) == "현재 열린 WIN5 라운드가 없습니다."


def test_info_formatter_preserves_missing_optional_values() -> None:
    info = _info()
    message = format_active_season_info(
        Win5ActiveSeasonInfo(
            season_id=info.season_id,
            season_name=info.season_name,
            season_status=info.season_status,
            starts_at=None,
            ends_at=None,
            total_round_count=0,
            open_round_count=0,
            season_score=0,
            top1_score=0,
        )
    )

    assert "시작: -\n종료: -" in message
    assert message.endswith("열린 라운드 확인: /win5 rounds")
