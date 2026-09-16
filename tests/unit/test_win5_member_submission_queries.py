"""Application boundary tests for WIN5 member submission queries."""

from win5_member_query_test_support import (
    PersonaStatus,
    QueryRunner,
    RecordingFactory,
    RecordingRepository,
    Win5AcceptedNormalSubmission,
    Win5AcceptedSpecialSubmission,
    Win5CancellableSubmissionUnavailableError,
    Win5MemberQueries,
    Win5MemberQueryIdentityError,
    Win5NormalSubmissionInvalidSourceError,
    Win5NormalSubmissionPick,
    Win5NormalSubmissionRoundChoice,
    Win5NormalSubmissionRoundUnavailableError,
    Win5NormalSubmissionSource,
    Win5SpecialSubmissionInvalidSourceError,
    Win5SpecialSubmissionPick,
    Win5SpecialSubmissionRoundChoice,
    Win5SpecialSubmissionRoundUnavailableError,
    Win5SpecialSubmissionSource,
    Win5SubmissionPick,
    Win5SubmissionStatus,
    Win5SubmissionTier,
    _cancellable_submission,
    _member,
    _normal_editor_source,
    _special_editor_source,
    pytest,
)


def test_normal_submission_choices_require_active_persona_and_use_read_only_uow() -> None:
    choices = (
        Win5NormalSubmissionRoundChoice(
            season_id=7,
            season_name="2026 하반기",
            round_id=11,
            round_name="Round 11",
            race_id=1101,
            race_name="Tokyo 11R",
        ),
    )
    repository = RecordingRepository(
        (),
        member=_member(),
        normal_round_choices=choices,
    )
    factory = RecordingFactory(repository)

    result = Win5MemberQueries(QueryRunner(factory)).search_normal_submission_rounds(
        discord_user_id="123",
        query="  Round  ",
        limit=25,
    )

    assert result == choices
    assert repository.member_discord_user_ids == ["123"]
    assert repository.normal_round_searches == [("Round", 25)]
    assert factory.created[0].commit_count == 0
    assert factory.created[0].rollback_count == 1


def test_cancellable_submission_queries_require_active_persona_and_use_read_only_uow() -> None:
    target = _cancellable_submission()
    repository = RecordingRepository(
        (),
        member=_member(),
        cancellable_submissions=(target,),
        cancellable_submission=target,
    )
    factory = RecordingFactory(repository)
    queries = Win5MemberQueries(QueryRunner(factory))

    choices = queries.search_cancellable_submissions(
        discord_user_id="123",
        query="  Round  ",
        limit=25,
    )
    selected = queries.get_cancellable_submission(
        discord_user_id="123",
        submission_id=501,
    )

    assert choices == (target,)
    assert selected == target
    assert repository.member_discord_user_ids == ["123", "123"]
    assert repository.cancellable_searches == [("persona-1", "Round", 25)]
    assert repository.cancellable_queries == [(501, "persona-1")]
    assert all(unit_of_work.commit_count == 0 for unit_of_work in factory.created)
    assert all(unit_of_work.rollback_count == 1 for unit_of_work in factory.created)


def test_cancellable_submission_query_rejects_stale_or_unowned_target() -> None:
    repository = RecordingRepository(
        (),
        member=_member(),
    )
    factory = RecordingFactory(repository)

    with pytest.raises(Win5CancellableSubmissionUnavailableError):
        Win5MemberQueries(QueryRunner(factory)).get_cancellable_submission(
            discord_user_id="123",
            submission_id=501,
        )

    assert repository.cancellable_queries == [(501, "persona-1")]
    assert factory.created[0].commit_count == 0
    assert factory.created[0].exited is True


def test_normal_submission_editor_prefills_partial_persona_owned_state_without_commit() -> None:
    submission = Win5NormalSubmissionSource(
        id=501,
        round_id=11,
        persona_id="persona-1",
        tier=Win5SubmissionTier.TOP5,
        status=Win5SubmissionStatus.ACCEPTED,
        active_marker=True,
        version=4,
        picks=(
            Win5SubmissionPick(
                id=601,
                submission_id=501,
                race_id=1101,
                race_entry_id=2001,
                position=1,
            ),
            Win5SubmissionPick(
                id=602,
                submission_id=501,
                race_id=1101,
                race_entry_id=2003,
                position=3,
            ),
        ),
    )
    repository = RecordingRepository(
        (),
        member=_member(),
        normal_editor_source=_normal_editor_source(submission=submission),
    )
    factory = RecordingFactory(repository)

    editor = Win5MemberQueries(QueryRunner(factory)).get_normal_submission_editor(
        discord_user_id="123",
        round_id=11,
    )

    assert editor.round_name == "Round 11"
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
    assert repository.normal_editor_queries == [(11, "persona-1")]
    assert factory.created[0].commit_count == 0
    assert factory.created[0].rollback_count == 1


def test_normal_submission_editor_rejects_inactive_identity_before_target_query() -> None:
    repository = RecordingRepository(
        (),
        member=_member(status=PersonaStatus.WITHDRAWN),
        normal_editor_source=_normal_editor_source(),
    )
    factory = RecordingFactory(repository)

    with pytest.raises(Win5MemberQueryIdentityError):
        Win5MemberQueries(QueryRunner(factory)).get_normal_submission_editor(
            discord_user_id="123",
            round_id=11,
        )

    assert repository.normal_editor_queries == []
    assert factory.created[0].commit_count == 0


def test_normal_submission_editor_rejects_unavailable_or_malformed_source() -> None:
    member = _member()
    unavailable_repository = RecordingRepository((), member=member)
    invalid_repository = RecordingRepository(
        (),
        member=member,
        normal_editor_source=_normal_editor_source(races=()),
    )

    with pytest.raises(Win5NormalSubmissionRoundUnavailableError):
        Win5MemberQueries(QueryRunner(RecordingFactory(unavailable_repository))).get_normal_submission_editor(
            discord_user_id="123",
            round_id=11,
        )
    with pytest.raises(Win5NormalSubmissionInvalidSourceError):
        Win5MemberQueries(QueryRunner(RecordingFactory(invalid_repository))).get_normal_submission_editor(
            discord_user_id="123",
            round_id=11,
        )


def test_special_submission_choices_and_partial_gate_prefill_use_read_only_uow() -> None:
    choices = (
        Win5SpecialSubmissionRoundChoice(
            season_id=7,
            season_name="2026 하반기",
            round_id=12,
            round_name="Special Round 12",
            race_count=3,
        ),
    )
    submission = Win5SpecialSubmissionSource(
        id=502,
        round_id=12,
        persona_id="persona-1",
        tier=Win5SubmissionTier.SPECIAL_WINNER,
        status=Win5SubmissionStatus.ACCEPTED,
        active_marker=True,
        version=6,
        picks=(
            Win5SubmissionPick(
                id=611,
                submission_id=502,
                race_id=1201,
                race_entry_id=None,
                position=1,
                gate_number=8,
            ),
            Win5SubmissionPick(
                id=612,
                submission_id=502,
                race_id=1203,
                race_entry_id=None,
                position=1,
                gate_number=99,
            ),
        ),
    )
    repository = RecordingRepository(
        (),
        member=_member(),
        special_round_choices=choices,
        special_editor_source=_special_editor_source(submission=submission),
    )
    factory = RecordingFactory(repository)
    queries = Win5MemberQueries(QueryRunner(factory))

    result_choices = queries.search_special_submission_rounds(
        discord_user_id="123",
        query="  Special  ",
    )
    editor = queries.get_special_submission_editor(
        discord_user_id="123",
        round_id=12,
    )

    assert result_choices == choices
    assert repository.special_round_searches == [("Special", 25)]
    assert editor.submission == Win5AcceptedSpecialSubmission(
        id=502,
        version=6,
        picks=(
            Win5SpecialSubmissionPick(race_id=1201, gate_number=8),
            Win5SpecialSubmissionPick(race_id=1203, gate_number=99),
        ),
    )
    assert editor.races[0].entries[0].name == "Do Deuce"
    assert repository.special_editor_queries == [(12, "persona-1")]
    assert all(unit_of_work.commit_count == 0 for unit_of_work in factory.created)
    assert all(unit_of_work.rollback_count == 1 for unit_of_work in factory.created)


def test_special_submission_editor_rejects_unavailable_or_malformed_source() -> None:
    member = _member()
    unavailable_repository = RecordingRepository((), member=member)
    invalid_repository = RecordingRepository(
        (),
        member=member,
        special_editor_source=_special_editor_source(races=()),
    )

    with pytest.raises(Win5SpecialSubmissionRoundUnavailableError):
        Win5MemberQueries(QueryRunner(RecordingFactory(unavailable_repository))).get_special_submission_editor(
            discord_user_id="123",
            round_id=12,
        )
    with pytest.raises(Win5SpecialSubmissionInvalidSourceError):
        Win5MemberQueries(QueryRunner(RecordingFactory(invalid_repository))).get_special_submission_editor(
            discord_user_id="123",
            round_id=12,
        )
