"""SQLite projection tests for WIN5 member submission queries."""

from win5_member_query_test_support import (
    NOW,
    RACE_SCHEDULED_AT,
    Base,
    DatabaseRuntime,
    DiscordAccountORM,
    GameAccountORM,
    PersonaORM,
    Win5AcceptedNormalSubmission,
    Win5AcceptedSpecialSubmission,
    Win5CancellableSubmission,
    Win5CancellableSubmissionUnavailableError,
    Win5NormalSubmissionPick,
    Win5RaceEntryOption,
    Win5RaceEntryORM,
    Win5RaceORM,
    Win5RoundORM,
    Win5RoundType,
    Win5SeasonORM,
    Win5SpecialSubmissionPick,
    Win5SubmissionORM,
    Win5SubmissionPickORM,
    Win5SubmissionTier,
    _game_account_row,
    _race_row,
    compose_win5_member_queries,
    create_engine,
    pytest,
)


def test_composed_normal_submission_editor_uses_linked_persona_and_prefills_versioned_picks() -> None:
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
                {
                    "id": 11,
                    "season_id": 7,
                    "type": "normal",
                    "status": "open",
                    "name": "Round 11",
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            )
            connection.execute(
                Win5RaceORM.__table__.insert(),
                _race_row(1101, 11, "Tokyo 11R", scheduled_at=RACE_SCHEDULED_AT),
            )
            connection.execute(
                Win5RaceEntryORM.__table__.insert(),
                [
                    {
                        "id": 2000 + gate_number,
                        "race_id": 1101,
                        "gate_number": gate_number,
                        "name": f"Horse {gate_number}",
                        "created_at": NOW,
                        "updated_at": NOW,
                    }
                    for gate_number in range(1, 9)
                ],
            )
            connection.execute(
                Win5SubmissionORM.__table__.insert(),
                [
                    {
                        "id": 500,
                        "round_id": 11,
                        "persona_id": "persona-1",
                        "tier": "TOP3",
                        "status": "cancelled",
                        "active_marker": None,
                        "version": 2,
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                    {
                        "id": 501,
                        "round_id": 11,
                        "persona_id": "persona-1",
                        "tier": "TOP5",
                        "status": "accepted",
                        "active_marker": True,
                        "version": 4,
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                ],
            )
            connection.execute(
                Win5SubmissionPickORM.__table__.insert(),
                [
                    {
                        "id": 601,
                        "submission_id": 501,
                        "race_id": 1101,
                        "race_entry_id": 2001,
                        "gate_number": None,
                        "position": 1,
                    },
                    {
                        "id": 602,
                        "submission_id": 501,
                        "race_id": 1101,
                        "race_entry_id": 2003,
                        "gate_number": None,
                        "position": 3,
                    },
                ],
            )

        queries = compose_win5_member_queries(runtime)
        choices = queries.search_normal_submission_rounds(
            discord_user_id="123",
            query="Round",
        )
        race_name_choices = queries.search_normal_submission_rounds(
            discord_user_id="123",
            query="Tokyo",
        )
        editor = queries.get_normal_submission_editor(
            discord_user_id="123",
            round_id=11,
        )
        cancellable = queries.search_cancellable_submissions(
            discord_user_id="123",
            query="501",
        )
        cancellable_target = queries.get_cancellable_submission(
            discord_user_id="123",
            submission_id=501,
        )

        assert [(choice.round_id, choice.race_id) for choice in choices] == [(11, 1101)]
        assert race_name_choices == choices
        assert editor.round_name == "Round 11"
        assert editor.race_name == "Tokyo 11R"
        assert [entry.gate_number for entry in editor.entries] == list(range(1, 9))
        assert editor.submission == Win5AcceptedNormalSubmission(
            id=501,
            tier=Win5SubmissionTier.TOP5,
            version=4,
            picks=(
                Win5NormalSubmissionPick(position=1, race_entry_id=2001),
                Win5NormalSubmissionPick(position=3, race_entry_id=2003),
            ),
        )
        assert cancellable == (
            Win5CancellableSubmission(
                season_id=7,
                season_name="2026 하반기",
                round_id=11,
                round_name="Round 11",
                round_type=Win5RoundType.NORMAL,
                submission_id=501,
                tier=Win5SubmissionTier.TOP5,
                version=4,
                pick_count=2,
            ),
        )
        assert cancellable_target == cancellable[0]
        with pytest.raises(Win5CancellableSubmissionUnavailableError):
            queries.get_cancellable_submission(
                discord_user_id="123",
                submission_id=500,
            )
    finally:
        runtime.dispose()


def test_composed_special_submission_editor_prefills_gate_picks_and_optional_references() -> None:
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
                {
                    "id": 12,
                    "season_id": 7,
                    "type": "special",
                    "status": "open",
                    "name": "Special Round 12",
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            )
            connection.execute(
                Win5RaceORM.__table__.insert(),
                [
                    _race_row(1203, 12, "Hanshin 12R"),
                    _race_row(1201, 12, "Tokyo 11R"),
                    _race_row(1202, 12, "Kyoto 10R"),
                ],
            )
            connection.execute(
                Win5RaceEntryORM.__table__.insert(),
                {
                    "id": 3008,
                    "race_id": 1201,
                    "gate_number": 8,
                    "name": "Do Deuce",
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            )
            connection.execute(
                Win5SubmissionORM.__table__.insert(),
                {
                    "id": 502,
                    "round_id": 12,
                    "persona_id": "persona-1",
                    "tier": "SPECIAL_WINNER",
                    "status": "accepted",
                    "active_marker": True,
                    "version": 6,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            )
            connection.execute(
                Win5SubmissionPickORM.__table__.insert(),
                [
                    {
                        "id": 611,
                        "submission_id": 502,
                        "race_id": 1201,
                        "race_entry_id": None,
                        "gate_number": 8,
                        "position": 1,
                    },
                    {
                        "id": 612,
                        "submission_id": 502,
                        "race_id": 1203,
                        "race_entry_id": None,
                        "gate_number": 99,
                        "position": 1,
                    },
                ],
            )

        queries = compose_win5_member_queries(runtime)
        choices = queries.search_special_submission_rounds(
            discord_user_id="123",
            query="Hanshin",
        )
        editor = queries.get_special_submission_editor(
            discord_user_id="123",
            round_id=12,
        )

        assert [(choice.round_id, choice.race_count) for choice in choices] == [(12, 3)]
        assert [race.id for race in editor.races] == [1201, 1202, 1203]
        assert editor.races[0].entries == (Win5RaceEntryOption(id=3008, gate_number=8, name="Do Deuce"),)
        assert editor.races[1].entries == ()
        assert editor.submission == Win5AcceptedSpecialSubmission(
            id=502,
            version=6,
            picks=(
                Win5SpecialSubmissionPick(race_id=1201, gate_number=8),
                Win5SpecialSubmissionPick(race_id=1203, gate_number=99),
            ),
        )
    finally:
        runtime.dispose()
