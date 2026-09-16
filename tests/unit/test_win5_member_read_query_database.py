"""SQLite projection tests for WIN5 member read queries."""

from win5_member_query_test_support import (
    NOW,
    RACE_SCHEDULED_AT,
    ROUND_CLOSES_AT,
    ROUND_OPENS_AT,
    UTC,
    Base,
    DatabaseRuntime,
    DiscordAccountORM,
    GameAccountORM,
    PersonaORM,
    Win5MemberQueryInvalidSourceError,
    Win5RaceEntryORM,
    Win5RaceORM,
    Win5RoundORM,
    Win5RoundType,
    Win5ScoreORM,
    Win5SeasonORM,
    Win5SeasonStatus,
    Win5StandingsSeasonUnavailableError,
    _game_account_row,
    _race_row,
    compose_win5_member_queries,
    create_engine,
    datetime,
    pytest,
)


def test_composed_sqlalchemy_query_returns_only_ordered_active_open_round_cards() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            connection.execute(
                Win5SeasonORM.__table__.insert(),
                [
                    {
                        "id": 1,
                        "name": "2026 하반기",
                        "status": "active",
                        "active_marker": True,
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                    {
                        "id": 2,
                        "name": "종료 시즌",
                        "status": "closed",
                        "active_marker": None,
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                ],
            )
            connection.execute(
                Win5RoundORM.__table__.insert(),
                [
                    {
                        "id": 11,
                        "season_id": 1,
                        "type": "special",
                        "status": "open",
                        "name": "제2회 특별전",
                        "opens_at": None,
                        "closes_at": None,
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                    {
                        "id": 10,
                        "season_id": 1,
                        "type": "normal",
                        "status": "open",
                        "name": "제1회 아리마 기념",
                        "opens_at": ROUND_OPENS_AT,
                        "closes_at": ROUND_CLOSES_AT,
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                    {
                        "id": 12,
                        "season_id": 1,
                        "type": "normal",
                        "status": "closed",
                        "name": "제3회 종료 라운드",
                        "opens_at": None,
                        "closes_at": None,
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                    {
                        "id": 20,
                        "season_id": 2,
                        "type": "normal",
                        "status": "open",
                        "name": "종료 시즌 라운드",
                        "opens_at": None,
                        "closes_at": None,
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                ],
            )
            connection.execute(
                Win5RaceORM.__table__.insert(),
                [
                    _race_row(100, 10, "아리마 기념", scheduled_at=RACE_SCHEDULED_AT),
                    _race_row(111, 11, "Race B"),
                    _race_row(110, 11, "Race A"),
                    _race_row(120, 12, "closed"),
                    _race_row(200, 20, "inactive"),
                ],
            )
            connection.execute(
                Win5RaceEntryORM.__table__.insert(),
                [
                    {
                        "id": 1002,
                        "race_id": 100,
                        "gate_number": 2,
                        "name": "Horse 2",
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                    {
                        "id": 1001,
                        "race_id": 100,
                        "gate_number": 1,
                        "name": "Horse 1",
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                    {
                        "id": 1107,
                        "race_id": 110,
                        "gate_number": 7,
                        "name": "Reference 7",
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                ],
            )

        rounds = compose_win5_member_queries(runtime).list_open_rounds()

        assert tuple(round_.id for round_ in rounds) == (10, 11)
        assert tuple(round_.name for round_ in rounds) == ("제1회 아리마 기념", "제2회 특별전")
        assert rounds[0].round_type == Win5RoundType.NORMAL
        assert rounds[0].opens_at == ROUND_OPENS_AT.replace(tzinfo=UTC)
        assert rounds[0].closes_at == ROUND_CLOSES_AT.replace(tzinfo=UTC)
        assert rounds[0].races[0].scheduled_at == RACE_SCHEDULED_AT.replace(tzinfo=UTC)
        assert tuple(entry.gate_number for entry in rounds[0].races[0].entries) == (1, 2)
        assert rounds[1].round_type == Win5RoundType.SPECIAL
        assert tuple(race.id for race in rounds[1].races) == (110, 111)
        assert tuple(entry.gate_number for entry in rounds[1].races[0].entries) == (7,)
        assert rounds[1].races[1].entries == ()
    finally:
        runtime.dispose()


def test_composed_member_bounded_queries_allow_25_and_fail_closed_on_26_open_rounds() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            connection.execute(
                PersonaORM.__table__.insert(),
                {
                    "id": "persona-1",
                    "display_name": "참가자",
                    "status": "normal",
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            )
            connection.execute(
                DiscordAccountORM.__table__.insert(),
                {
                    "id": 1,
                    "discord_user_id": "123",
                    "persona_id": "persona-1",
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            )
            connection.execute(
                GameAccountORM.__table__.insert(),
                _game_account_row(id_=1, persona_id="persona-1", uma_pid="100000001"),
            )
            connection.execute(
                Win5SeasonORM.__table__.insert(),
                {
                    "id": 7,
                    "name": "2026 하반기",
                    "status": "active",
                    "active_marker": True,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            )
            connection.execute(
                Win5RoundORM.__table__.insert(),
                [
                    {
                        "id": round_id,
                        "season_id": 7,
                        "type": "normal" if round_id % 2 else "special",
                        "status": "open",
                        "name": f"Round {round_id}",
                        "created_at": NOW,
                        "updated_at": NOW,
                    }
                    for round_id in range(1, 26)
                ],
            )

        queries = compose_win5_member_queries(runtime)
        assert queries.get_active_season_info(discord_user_id="123").open_round_count == 25
        assert len(queries.list_open_rounds()) == 25
        assert queries.search_normal_submission_rounds(discord_user_id="123") == ()
        assert queries.search_special_submission_rounds(discord_user_id="123") == ()
        assert queries.search_cancellable_submissions(discord_user_id="123") == ()

        with runtime.engine.begin() as connection:
            connection.execute(
                Win5RoundORM.__table__.insert(),
                {
                    "id": 26,
                    "season_id": 7,
                    "type": "special",
                    "status": "open",
                    "name": "Round 26",
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            )

        with pytest.raises(Win5MemberQueryInvalidSourceError, match="more than 25 open Rounds"):
            queries.get_active_season_info(discord_user_id="123")
        with pytest.raises(Win5MemberQueryInvalidSourceError, match="more than 25 open Rounds"):
            queries.list_open_rounds()
        with pytest.raises(Win5MemberQueryInvalidSourceError, match="more than 25 open Rounds"):
            queries.search_normal_submission_rounds(discord_user_id="123")
        with pytest.raises(Win5MemberQueryInvalidSourceError, match="more than 25 open Rounds"):
            queries.search_special_submission_rounds(discord_user_id="123")
        with pytest.raises(Win5MemberQueryInvalidSourceError, match="more than 25 open Rounds"):
            queries.search_cancellable_submissions(discord_user_id="123")
    finally:
        runtime.dispose()


def test_composed_active_season_info_uses_persona_score_round_counts_and_zero_default() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            connection.execute(
                PersonaORM.__table__.insert(),
                [
                    {
                        "id": "persona-scored",
                        "display_name": "득점자",
                        "status": "normal",
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                    {
                        "id": "persona-zero",
                        "display_name": "신규 참가자",
                        "status": "normal",
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                ],
            )
            connection.execute(
                DiscordAccountORM.__table__.insert(),
                [
                    {
                        "id": 1,
                        "discord_user_id": "123456789",
                        "persona_id": "persona-scored",
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                    {
                        "id": 2,
                        "discord_user_id": "987654321",
                        "persona_id": "persona-zero",
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                ],
            )
            connection.execute(
                GameAccountORM.__table__.insert(),
                [
                    _game_account_row(id_=1, persona_id="persona-scored", uma_pid="100000001"),
                    _game_account_row(id_=2, persona_id="persona-zero", uma_pid="100000002"),
                ],
            )
            connection.execute(
                Win5SeasonORM.__table__.insert(),
                {
                    "id": 7,
                    "name": "2026 하반기",
                    "status": "active",
                    "active_marker": True,
                    "starts_at": datetime(2026, 8, 25, 0, 0),
                    "ends_at": None,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            )
            connection.execute(
                Win5RoundORM.__table__.insert(),
                [
                    {
                        "id": 70,
                        "season_id": 7,
                        "type": "normal",
                        "status": "open",
                        "name": "제1회 아리마 기념",
                        "opens_at": None,
                        "closes_at": None,
                        "created_at": NOW,
                        "updated_at": datetime(2026, 8, 27, 0, 0),
                    },
                    {
                        "id": 71,
                        "season_id": 7,
                        "type": "special",
                        "status": "open",
                        "name": "제2회 브리더스컵 데이",
                        "opens_at": None,
                        "closes_at": None,
                        "created_at": NOW,
                        "updated_at": datetime(2026, 8, 26, 0, 0),
                    },
                    {
                        "id": 72,
                        "season_id": 7,
                        "type": "normal",
                        "status": "closed",
                        "name": "제3회 종료 라운드",
                        "opens_at": None,
                        "closes_at": None,
                        "created_at": NOW,
                        "updated_at": datetime(2026, 8, 28, 0, 0),
                    },
                ],
            )
            connection.execute(
                Win5ScoreORM.__table__.insert(),
                {
                    "season_id": 7,
                    "persona_id": "persona-scored",
                    "season_score": 21,
                    "top1_score": 8,
                    "updated_at": NOW,
                },
            )

        queries = compose_win5_member_queries(runtime)
        info = queries.get_active_season_info(discord_user_id="123456789")
        zero_score_info = queries.get_active_season_info(discord_user_id="987654321")

        assert info.season_id == 7
        assert info.season_name == "2026 하반기"
        assert info.season_status == Win5SeasonStatus.ACTIVE
        assert info.starts_at == datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
        assert info.ends_at is None
        assert info.total_round_count == 3
        assert info.open_round_count == 2
        assert (info.season_score, info.top1_score) == (21, 8)
        assert (zero_score_info.season_score, zero_score_info.top1_score) == (0, 0)
    finally:
        runtime.dispose()


def test_composed_standings_use_persona_scores_competition_rank_and_available_seasons() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            connection.execute(
                Win5SeasonORM.__table__.insert(),
                [
                    {
                        "id": 10,
                        "name": "2026 하반기",
                        "status": "active",
                        "active_marker": True,
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                    {
                        "id": 9,
                        "name": "2026 하반기 예선",
                        "status": "closed",
                        "active_marker": None,
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                    {
                        "id": 11,
                        "name": "2026 하반기 draft",
                        "status": "draft",
                        "active_marker": None,
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                    {
                        "id": 12,
                        "name": "2026 하반기 cancelled",
                        "status": "cancelled",
                        "active_marker": None,
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                ],
            )
            connection.execute(
                PersonaORM.__table__.insert(),
                [
                    {
                        "id": "persona-a",
                        "display_name": "A",
                        "status": "normal",
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                    {
                        "id": "persona-b",
                        "display_name": "B",
                        "status": "normal",
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                    {
                        "id": "persona-c",
                        "display_name": "C",
                        "status": "normal",
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                    {
                        "id": "persona-d",
                        "display_name": "탈퇴 후 역사 점수",
                        "status": "withdrawn",
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                ],
            )
            connection.execute(
                Win5ScoreORM.__table__.insert(),
                [
                    {
                        "season_id": 10,
                        "persona_id": "persona-a",
                        "season_score": 30,
                        "top1_score": 1,
                        "updated_at": NOW,
                    },
                    {
                        "season_id": 10,
                        "persona_id": "persona-b",
                        "season_score": 20,
                        "top1_score": 5,
                        "updated_at": NOW,
                    },
                    {
                        "season_id": 10,
                        "persona_id": "persona-c",
                        "season_score": 20,
                        "top1_score": 5,
                        "updated_at": NOW,
                    },
                    {
                        "season_id": 10,
                        "persona_id": "persona-d",
                        "season_score": 10,
                        "top1_score": 0,
                        "updated_at": NOW,
                    },
                ],
            )

        queries = compose_win5_member_queries(runtime)

        assert tuple(choice.id for choice in queries.search_standings_seasons()) == (10, 9)
        assert tuple(choice.id for choice in queries.search_standings_seasons(query="예선")) == (9,)

        season = queries.get_standings(season_id=10, ranking="season")
        assert tuple((entry.display_name, entry.score, entry.rank) for entry in season.entries) == (
            ("A", 30, 1),
            ("B", 20, 2),
            ("C", 20, 2),
            ("탈퇴 후 역사 점수", 10, 4),
        )

        top1 = queries.get_standings(season_id=10, ranking="top1")
        assert tuple((entry.display_name, entry.score, entry.rank) for entry in top1.entries) == (
            ("B", 5, 1),
            ("C", 5, 1),
            ("A", 1, 3),
            ("탈퇴 후 역사 점수", 0, 4),
        )
        assert queries.get_standings(season_id=9).entries == ()
        with pytest.raises(Win5StandingsSeasonUnavailableError):
            queries.get_standings(season_id=11)
        with pytest.raises(Win5StandingsSeasonUnavailableError):
            queries.get_standings(season_id=12)
    finally:
        runtime.dispose()
