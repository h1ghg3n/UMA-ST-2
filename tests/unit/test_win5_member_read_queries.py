"""Application boundary tests for WIN5 member read queries."""

from win5_member_query_test_support import (
    PersonaStatus,
    QueryRunner,
    RecordingFactory,
    RecordingRepository,
    RecordingUnitOfWork,
    Win5ActiveSeasonUnavailableError,
    Win5MemberPersona,
    Win5MemberQueries,
    Win5MemberQueryApprovalPendingError,
    Win5MemberQueryIdentityError,
    Win5MemberQueryInvalidSourceError,
    Win5OpenRoundCard,
    Win5RaceCard,
    Win5RoundType,
    Win5SeasonChoice,
    Win5SeasonStatus,
    Win5StandingEntry,
    Win5Standings,
    Win5StandingScore,
    Win5StandingsSeasonUnavailableError,
    Win5StandingsSource,
    _active_season_info,
    _member,
    pytest,
)


def test_active_season_info_uses_linked_persona_query_without_commit() -> None:
    expected = _active_season_info()
    repository = RecordingRepository(
        (),
        member=_member(),
        info=expected,
    )
    factory = RecordingFactory(repository)

    result = Win5MemberQueries(QueryRunner(factory)).get_active_season_info(discord_user_id="123456789")

    assert result == expected
    assert repository.member_discord_user_ids == ["123456789"]
    assert repository.info_persona_ids == ["persona-1"]
    assert factory.created[0].commit_count == 0
    assert factory.created[0].rollback_count == 1


@pytest.mark.parametrize(
    "member",
    [
        None,
        _member(status=PersonaStatus.EXPELLED),
        _member(status=PersonaStatus.WITHDRAWN),
        _member(has_eligible_game_account=False),
    ],
)
def test_active_season_info_rejects_missing_or_ineligible_persona(member: Win5MemberPersona | None) -> None:
    repository = RecordingRepository((), member=member, info=_active_season_info())

    with pytest.raises(Win5MemberQueryIdentityError, match="non-NULL PID GameAccount"):
        Win5MemberQueries(QueryRunner(RecordingFactory(repository))).get_active_season_info(discord_user_id="123456789")

    assert repository.info_persona_ids == []


def test_pending_approval_persona_can_read_status_but_not_open_mutation_editor() -> None:
    repository = RecordingRepository(
        (),
        member=_member(
            status=PersonaStatus.PENDING_APPROVAL,
            has_eligible_game_account=False,
        ),
        info=_active_season_info(),
    )
    queries = Win5MemberQueries(QueryRunner(RecordingFactory(repository)))

    assert queries.get_active_season_info(discord_user_id="123456789") == _active_season_info()
    with pytest.raises(Win5MemberQueryApprovalPendingError, match="approval is pending"):
        queries.search_normal_submission_rounds(discord_user_id="123456789")

    assert repository.info_persona_ids == ["persona-1"]


def test_active_season_info_reports_missing_active_season() -> None:
    repository = RecordingRepository(
        (),
        member=_member(),
    )

    with pytest.raises(Win5ActiveSeasonUnavailableError, match="No active WIN5 Season"):
        Win5MemberQueries(QueryRunner(RecordingFactory(repository))).get_active_season_info(discord_user_id="123456789")


def test_active_season_info_fails_closed_on_observed_open_round_overflow() -> None:
    repository = RecordingRepository(
        (),
        member=_member(),
        info=_active_season_info(open_round_count=26),
    )

    with pytest.raises(Win5MemberQueryInvalidSourceError, match="more than 25 open Rounds"):
        Win5MemberQueries(QueryRunner(RecordingFactory(repository))).get_active_season_info(discord_user_id="123456789")

    assert repository.active_open_overflow_query_count == 0


@pytest.mark.parametrize("discord_user_id", [None, "", "1" * 33])
def test_active_season_info_rejects_invalid_discord_user_id_without_opening_uow(discord_user_id: object) -> None:
    factory = RecordingFactory(RecordingRepository(()))

    with pytest.raises(ValueError, match="discord_user_id"):
        Win5MemberQueries(QueryRunner(factory)).get_active_season_info(
            discord_user_id=discord_user_id  # type: ignore[arg-type]
        )

    assert factory.created == []


def test_member_round_catalog_uses_bounded_query_runner_without_commit() -> None:
    expected = (
        Win5OpenRoundCard(
            id=10,
            season_id=1,
            season_name="2026 하반기",
            round_type=Win5RoundType.NORMAL,
            name="제1회 아리마 기념",
            opens_at=None,
            closes_at=None,
            races=(Win5RaceCard(id=100, name="아리마 기념", scheduled_at=None),),
        ),
    )
    repository = RecordingRepository(expected)
    factory = RecordingFactory(repository)
    queries = Win5MemberQueries(QueryRunner(factory))

    assert queries.list_open_rounds(limit=7) == expected
    assert repository.limits == [7]
    assert factory.created == [
        RecordingUnitOfWork(
            repository,
            entered=True,
            exited=True,
            commit_count=0,
            rollback_count=1,
        )
    ]


def test_open_round_member_selectors_fail_closed_before_truncating_overflow() -> None:
    repository = RecordingRepository(
        (),
        member=_member(),
        active_open_overflow=True,
    )
    queries = Win5MemberQueries(QueryRunner(RecordingFactory(repository)))

    with pytest.raises(Win5MemberQueryInvalidSourceError, match="more than 25 open Rounds"):
        queries.list_open_rounds()
    with pytest.raises(Win5MemberQueryInvalidSourceError, match="more than 25 open Rounds"):
        queries.search_normal_submission_rounds(discord_user_id="123")
    with pytest.raises(Win5MemberQueryInvalidSourceError, match="more than 25 open Rounds"):
        queries.search_special_submission_rounds(discord_user_id="123")
    with pytest.raises(Win5MemberQueryInvalidSourceError, match="more than 25 open Rounds"):
        queries.search_cancellable_submissions(discord_user_id="123")

    assert repository.active_open_overflow_query_count == 4
    assert repository.limits == []
    assert repository.normal_round_searches == []
    assert repository.special_round_searches == []
    assert repository.cancellable_searches == []


@pytest.mark.parametrize("limit", [True, 0, 101, 1.5])
def test_member_round_catalog_rejects_invalid_bounds_without_opening_uow(limit: object) -> None:
    repository = RecordingRepository(())
    factory = RecordingFactory(repository)
    queries = Win5MemberQueries(QueryRunner(factory))

    with pytest.raises(ValueError, match="1 through 100"):
        queries.list_open_rounds(limit=limit)  # type: ignore[arg-type]

    assert factory.created == []


def test_standings_queries_use_bounded_query_runner_without_commit() -> None:
    choices = (Win5SeasonChoice(id=7, name="2026 하반기", status=Win5SeasonStatus.ACTIVE),)
    expected = Win5Standings(
        season_id=7,
        season_name="2026 하반기",
        season_status=Win5SeasonStatus.ACTIVE,
        ranking="season",
        entries=(
            Win5StandingEntry(
                rank=1,
                persona_id="persona-a",
                display_name="A",
                score=30,
            ),
            Win5StandingEntry(
                rank=2,
                persona_id="persona-b",
                display_name="B",
                score=20,
            ),
            Win5StandingEntry(
                rank=2,
                persona_id="persona-c",
                display_name="C",
                score=20,
            ),
            Win5StandingEntry(
                rank=4,
                persona_id="persona-d",
                display_name="D",
                score=10,
            ),
        ),
    )
    source = Win5StandingsSource(
        season_id=7,
        season_name="2026 하반기",
        season_status=Win5SeasonStatus.ACTIVE,
        ranking="season",
        scores=(
            Win5StandingScore(
                persona_id="persona-c",
                display_name="C",
                score=20,
            ),
            Win5StandingScore(
                persona_id="persona-d",
                display_name="D",
                score=10,
            ),
            Win5StandingScore(
                persona_id="persona-a",
                display_name="A",
                score=30,
            ),
            Win5StandingScore(
                persona_id="persona-b",
                display_name="B",
                score=20,
            ),
        ),
    )
    repository = RecordingRepository((), season_choices=choices, standings_source=source)
    factory = RecordingFactory(repository)
    queries = Win5MemberQueries(QueryRunner(factory))

    assert queries.search_standings_seasons(query="  하반기  ", limit=10) == choices
    assert queries.get_standings(season_id=7, ranking="season", limit=50) == expected
    assert repository.season_searches == [("하반기", 10)]
    assert repository.standings_queries == [(7, "season", 50)]
    assert all(unit_of_work.commit_count == 0 for unit_of_work in factory.created)
    assert all(unit_of_work.rollback_count == 1 for unit_of_work in factory.created)


@pytest.mark.parametrize(
    ("method", "kwargs", "message"),
    [
        ("search", {"query": None}, "query"),
        ("search", {"query": "x" * 101}, "query"),
        ("search", {"limit": 26}, "1 through 25"),
        ("standings", {"season_id": True}, "positive integer"),
        ("standings", {"season_id": 0}, "positive integer"),
        ("standings", {"season_id": 1, "ranking": "overall"}, "season.*top1"),
        ("standings", {"season_id": 1, "limit": 101}, "1 through 100"),
    ],
)
def test_standings_queries_reject_invalid_inputs_without_opening_uow(
    method: str,
    kwargs: dict[str, object],
    message: str,
) -> None:
    factory = RecordingFactory(RecordingRepository(()))
    queries = Win5MemberQueries(QueryRunner(factory))

    with pytest.raises(ValueError, match=message):
        if method == "search":
            queries.search_standings_seasons(**kwargs)  # type: ignore[arg-type]
        else:
            queries.get_standings(**kwargs)  # type: ignore[arg-type]

    assert factory.created == []


def test_standings_reports_unavailable_season_as_expected_query_error() -> None:
    repository = RecordingRepository(())

    with pytest.raises(Win5StandingsSeasonUnavailableError, match="active or closed"):
        Win5MemberQueries(QueryRunner(RecordingFactory(repository))).get_standings(season_id=7)

    assert repository.standings_queries == [(7, "season", 100)]
