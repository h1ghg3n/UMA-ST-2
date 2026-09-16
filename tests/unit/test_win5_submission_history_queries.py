"""WIN5 submission-history query application and SQLAlchemy slice tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest
from sqlalchemy import create_engine

from uma_st2.application.execution import QueryRunner
from uma_st2.application.win5 import (
    Win5MemberPersona,
    Win5MemberQueryIdentityError,
    Win5MemberSubmission,
    Win5MemberSubmissionRound,
    Win5MemberSubmissionRoundPageSource,
    Win5MemberSubmissionRoundSummary,
    Win5MemberSubmissionsDashboardSource,
    Win5RaceCard,
    Win5SpecialSubmissionJudgement,
    Win5SubmissionHistoryQueries,
    Win5SubmissionsInvalidSourceError,
    Win5SubmissionsUnavailableError,
)
from uma_st2.compose import compose_win5_submission_history_queries
from uma_st2.domain.identity import PersonaStatus
from uma_st2.domain.win5 import (
    WIN5_NORMAL_REWARD_POLICY_VERSION,
    WIN5_NORMAL_SCORING_POLICY_VERSION,
    WIN5_SPECIAL_REWARD_POLICY_VERSION,
    WIN5_SPECIAL_SCORING_POLICY_VERSION,
    WIN5_SPECIAL_VOID_SCORING_POLICY_VERSION,
    Win5JudgementOutcome,
    Win5NormalResultPlacement,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SpecialResultWinner,
    Win5SubmissionStatus,
    Win5SubmissionTier,
    fingerprint_normal_result,
    fingerprint_special_result,
)
from uma_st2.infrastructure.database import Base, DatabaseRuntime
from uma_st2.infrastructure.database.orm import (
    DiscordAccountORM,
    GameAccountORM,
    PersonaORM,
    Win5RaceEntryORM,
    Win5RaceORM,
    Win5ResultORM,
    Win5RoundORM,
    Win5ScoreEventItemORM,
    Win5ScoreEventORM,
    Win5SeasonORM,
    Win5SubmissionORM,
    Win5SubmissionPickORM,
)

NOW = datetime(2026, 8, 25, 0, 0)


def _member(
    *,
    status: PersonaStatus = PersonaStatus.NORMAL,
    has_eligible_game_account: bool = True,
) -> Win5MemberPersona:
    return Win5MemberPersona(
        id="persona-1",
        status=status,
        has_eligible_game_account=has_eligible_game_account,
    )


def _game_account_row(
    *,
    id_: int,
    persona_id: str,
    uma_pid: str,
) -> dict[str, object]:
    return {
        "id": id_,
        "persona_id": persona_id,
        "game_region": "jp",
        "uma_pid": uma_pid,
        "nickname": f"Account {id_}",
        "affiliation": None,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _race_row(
    id_: int,
    round_id: int,
    name: str,
    *,
    scheduled_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "id": id_,
        "round_id": round_id,
        "name": name,
        "scheduled_at": scheduled_at,
        "created_at": NOW,
        "updated_at": NOW,
    }


class RecordingRepository:
    def __init__(
        self,
        *,
        member: Win5MemberPersona | None = None,
        dashboard_source: Win5MemberSubmissionsDashboardSource | None = None,
        history_page_source: Win5MemberSubmissionRoundPageSource | None = None,
    ) -> None:
        self.member = member
        self.dashboard_source = dashboard_source
        self.history_page_source = history_page_source
        self.member_discord_user_ids: list[str] = []
        self.dashboard_queries: list[tuple[str, int, int, int]] = []
        self.history_page_queries: list[tuple[str, int, int, int, int, int]] = []

    def find_member_persona(self, *, discord_user_id: str) -> Win5MemberPersona | None:
        self.member_discord_user_ids.append(discord_user_id)
        return self.member

    def get_submissions_dashboard_source(
        self,
        *,
        persona_id: str,
        open_limit: int,
        scored_limit: int,
        cancelled_limit: int,
    ) -> Win5MemberSubmissionsDashboardSource | None:
        self.dashboard_queries.append((persona_id, open_limit, scored_limit, cancelled_limit))
        return self.dashboard_source

    def get_submission_history_round_source(
        self,
        *,
        persona_id: str,
        round_id: int,
        submission_offset: int,
        submission_limit: int,
        race_offset: int,
        race_limit: int,
    ) -> Win5MemberSubmissionRoundPageSource | None:
        self.history_page_queries.append(
            (
                persona_id,
                round_id,
                submission_offset,
                submission_limit,
                race_offset,
                race_limit,
            )
        )
        return self.history_page_source


@dataclass
class RecordingUnitOfWork:
    win5_submission_history_queries: RecordingRepository
    entered: bool = False
    exited: bool = False
    commit_count: int = 0
    rollback_count: int = 0

    def __enter__(self) -> RecordingUnitOfWork:
        self.entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.exited = True
        return False

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


class RecordingFactory:
    def __init__(self, repository: RecordingRepository) -> None:
        self.repository = repository
        self.created: list[RecordingUnitOfWork] = []

    def __call__(self) -> RecordingUnitOfWork:
        unit_of_work = RecordingUnitOfWork(self.repository)
        self.created.append(unit_of_work)
        return unit_of_work


def _round_summary(
    *,
    id_: int = 12,
    status: Win5RoundStatus = Win5RoundStatus.OPEN,
    submission_count: int = 0,
) -> Win5MemberSubmissionRoundSummary:
    return Win5MemberSubmissionRoundSummary(
        id=id_,
        season_id=7,
        round_type=Win5RoundType.SPECIAL,
        status=status,
        name=f"Round {id_}",
        submission_count=submission_count,
    )


def test_submissions_dashboard_is_child_free_and_uses_query_runner_rollback() -> None:
    source = Win5MemberSubmissionsDashboardSource(
        season_id=7,
        season_name="2026 하반기",
        season_status=Win5SeasonStatus.ACTIVE,
        open_round_overflow=False,
        open_rounds=(_round_summary(),),
        scored_rounds=(
            _round_summary(
                id_=20,
                status=Win5RoundStatus.SCORED,
                submission_count=2,
            ),
        ),
        cancelled_rounds=(
            _round_summary(
                id_=30,
                status=Win5RoundStatus.CANCELLED,
                submission_count=1,
            ),
        ),
    )
    repository = RecordingRepository(
        member=_member(),
        dashboard_source=source,
    )
    factory = RecordingFactory(repository)

    dashboard = Win5SubmissionHistoryQueries(QueryRunner(factory)).get_submissions_dashboard(discord_user_id="123")

    assert dashboard.open_rounds == source.open_rounds
    assert dashboard.scored_rounds == source.scored_rounds
    assert dashboard.cancelled_rounds == source.cancelled_rounds
    assert isinstance(dashboard.open_rounds[0], Win5MemberSubmissionRoundSummary)
    assert not hasattr(dashboard.open_rounds[0], "races")
    assert not hasattr(dashboard.open_rounds[0], "submissions")
    assert repository.dashboard_queries == [("persona-1", 25, 25, 25)]
    assert factory.created[0].commit_count == 0
    assert factory.created[0].rollback_count == 1


def test_submissions_dashboard_requires_eligible_persona_and_fails_closed_on_open_overflow() -> None:
    overflow = Win5MemberSubmissionsDashboardSource(
        season_id=7,
        season_name="2026 하반기",
        season_status=Win5SeasonStatus.ACTIVE,
        open_round_overflow=True,
    )
    inactive_repository = RecordingRepository(
        member=_member(status=PersonaStatus.WITHDRAWN),
        dashboard_source=overflow,
    )
    with pytest.raises(Win5MemberQueryIdentityError):
        Win5SubmissionHistoryQueries(QueryRunner(RecordingFactory(inactive_repository))).get_submissions_dashboard(
            discord_user_id="123"
        )
    assert inactive_repository.dashboard_queries == []

    no_account_repository = RecordingRepository(
        member=_member(has_eligible_game_account=False),
        dashboard_source=overflow,
    )
    with pytest.raises(Win5MemberQueryIdentityError, match="non-NULL PID GameAccount"):
        Win5SubmissionHistoryQueries(QueryRunner(RecordingFactory(no_account_repository))).get_submissions_dashboard(
            discord_user_id="123"
        )
    assert no_account_repository.dashboard_queries == []

    active_repository = RecordingRepository(
        member=_member(),
        dashboard_source=overflow,
    )
    with pytest.raises(Win5SubmissionsInvalidSourceError):
        Win5SubmissionHistoryQueries(QueryRunner(RecordingFactory(active_repository))).get_submissions_dashboard(
            discord_user_id="123"
        )
    assert active_repository.dashboard_queries == [("persona-1", 25, 25, 25)]


def test_pending_approval_persona_can_read_existing_submission_history() -> None:
    source = Win5MemberSubmissionsDashboardSource(
        season_id=7,
        season_name="2026 하반기",
        season_status=Win5SeasonStatus.ACTIVE,
        open_round_overflow=False,
    )
    repository = RecordingRepository(
        member=_member(
            status=PersonaStatus.PENDING_APPROVAL,
            has_eligible_game_account=False,
        ),
        dashboard_source=source,
    )

    dashboard = Win5SubmissionHistoryQueries(QueryRunner(RecordingFactory(repository))).get_submissions_dashboard(
        discord_user_id="123"
    )

    assert dashboard.season_id == 7
    assert repository.dashboard_queries == [("persona-1", 25, 25, 25)]


def test_submission_history_page_uses_bounded_query_runner_and_public_page_shape() -> None:
    submission = Win5MemberSubmission(
        id=501,
        round_id=12,
        persona_id="persona-1",
        tier=Win5SubmissionTier.SPECIAL_WINNER,
        status=Win5SubmissionStatus.ACCEPTED,
        active_marker=True,
        version=1,
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
        updated_at=datetime(2026, 8, 25, tzinfo=UTC),
    )
    round_ = Win5MemberSubmissionRound(
        id=12,
        season_id=7,
        round_type=Win5RoundType.SPECIAL,
        status=Win5RoundStatus.OPEN,
        name="Special",
        opens_at=None,
        closes_at=None,
        races=(Win5RaceCard(id=1201, name="Race", scheduled_at=None),),
        submissions=(submission,),
    )
    source = Win5MemberSubmissionRoundPageSource(
        season_id=7,
        season_name="2026 하반기",
        season_status=Win5SeasonStatus.ACTIVE,
        round=round_,
        total_submission_count=1,
        submission_offset=0,
        submission_limit=5,
        total_race_count=1,
        race_offset=0,
        race_limit=5,
    )
    repository = RecordingRepository(
        member=_member(),
        history_page_source=source,
    )
    factory = RecordingFactory(repository)

    page = Win5SubmissionHistoryQueries(QueryRunner(factory)).get_submission_history_round(
        discord_user_id="123",
        round_id=12,
        submission_offset=0,
        race_offset=0,
    )

    assert page.season_id == 7
    assert page.season_name == "2026 하반기"
    assert page.round == round_
    assert (
        page.total_submission_count,
        page.submission_offset,
        page.submission_limit,
        page.total_race_count,
        page.race_offset,
        page.race_limit,
    ) == (1, 0, 5, 1, 0, 5)
    assert repository.history_page_queries == [("persona-1", 12, 0, 5, 0, 5)]
    assert factory.created[0].commit_count == 0
    assert factory.created[0].rollback_count == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"submission_offset": -1, "race_offset": 0}, "submission_offset"),
        ({"submission_offset": 0, "submission_limit": 6, "race_offset": 0}, "submission_limit"),
        ({"submission_offset": 0, "race_offset": -1}, "race_offset"),
        ({"submission_offset": 0, "race_offset": 0, "race_limit": 6}, "race_limit"),
    ],
)
def test_submission_history_page_rejects_invalid_offsets_and_limits_before_uow(
    kwargs: dict[str, int],
    message: str,
) -> None:
    factory = RecordingFactory(RecordingRepository())
    with pytest.raises(ValueError, match=message):
        Win5SubmissionHistoryQueries(QueryRunner(factory)).get_submission_history_round(
            discord_user_id="123",
            round_id=12,
            **kwargs,
        )
    assert factory.created == []


def test_submission_history_page_reports_stale_target_as_unavailable() -> None:
    repository = RecordingRepository(
        member=_member(),
    )
    with pytest.raises(Win5SubmissionsUnavailableError):
        Win5SubmissionHistoryQueries(QueryRunner(RecordingFactory(repository))).get_submission_history_round(
            discord_user_id="123",
            round_id=12,
            submission_offset=0,
            race_offset=0,
        )


def _seed_dashboard_identity_and_seasons(connection) -> None:
    connection.execute(
        PersonaORM.__table__.insert(),
        [
            {
                "id": "persona-1",
                "display_name": "참가자",
                "status": "normal",
                "created_at": NOW,
                "updated_at": NOW,
            },
            {
                "id": "persona-2",
                "display_name": "다른 참가자",
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
                "discord_user_id": "123",
                "persona_id": "persona-1",
                "created_at": NOW,
                "updated_at": NOW,
            },
            {
                "id": 2,
                "discord_user_id": "456",
                "persona_id": "persona-2",
                "created_at": NOW,
                "updated_at": NOW,
            },
        ],
    )
    connection.execute(
        GameAccountORM.__table__.insert(),
        [
            _game_account_row(id_=1, persona_id="persona-1", uma_pid="100000001"),
            _game_account_row(id_=2, persona_id="persona-2", uma_pid="100000002"),
        ],
    )
    connection.execute(
        Win5SeasonORM.__table__.insert(),
        [
            {
                "id": 7,
                "name": "2026 하반기",
                "status": "active",
                "active_marker": True,
                "created_at": NOW,
                "updated_at": NOW,
            },
            {
                "id": 6,
                "name": "2026 상반기",
                "status": "closed",
                "active_marker": None,
                "created_at": NOW,
                "updated_at": NOW,
            },
        ],
    )


def test_composed_dashboard_counts_multi_race_special_once_in_25_open_rounds() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            _seed_dashboard_identity_and_seasons(connection)
            connection.execute(
                Win5RoundORM.__table__.insert(),
                [
                    *[
                        {
                            "id": round_id,
                            "season_id": 7,
                            "type": "normal" if round_id % 2 else "special",
                            "status": "open",
                            "name": f"Open {round_id}",
                            "created_at": datetime(2026, 8, round_id),
                            "updated_at": NOW,
                        }
                        for round_id in range(1, 26)
                    ],
                    *[
                        {
                            "id": round_id,
                            "season_id": 7,
                            "type": "special",
                            "status": "scored",
                            "name": f"Scored {round_id}",
                            "created_at": datetime(2026, 7, round_id - 99),
                            "updated_at": NOW,
                        }
                        for round_id in range(100, 126)
                    ],
                    {
                        "id": 200,
                        "season_id": 6,
                        "type": "special",
                        "status": "open",
                        "name": "Old Open",
                        "created_at": NOW,
                        "updated_at": NOW,
                    },
                ],
            )
            connection.execute(
                Win5SubmissionORM.__table__.insert(),
                [
                    {
                        "id": round_id * 10,
                        "round_id": round_id,
                        "persona_id": "persona-1",
                        "tier": "SPECIAL_WINNER",
                        "status": "accepted",
                        "active_marker": True,
                        "version": 1,
                        "created_at": NOW,
                        "updated_at": NOW,
                    }
                    for round_id in range(100, 126)
                ],
            )
            connection.execute(
                Win5SubmissionORM.__table__.insert(),
                {
                    "id": 9999,
                    "round_id": 1,
                    "persona_id": "persona-2",
                    "tier": "SPECIAL_WINNER",
                    "status": "accepted",
                    "active_marker": True,
                    "version": 1,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            )
            connection.execute(
                Win5RaceORM.__table__.insert(),
                [
                    {
                        "id": race_id,
                        "round_id": 2,
                        "name": f"Special Race {race_id}",
                        "created_at": NOW,
                        "updated_at": NOW,
                    }
                    for race_id in range(201, 206)
                ],
            )

        dashboard = compose_win5_submission_history_queries(runtime).get_submissions_dashboard(discord_user_id="123")

        assert [summary.id for summary in dashboard.open_rounds] == list(range(1, 26))
        assert sum(summary.id == 2 for summary in dashboard.open_rounds) == 1
        assert dashboard.open_rounds[0].submission_count == 0
        assert all(not hasattr(summary, "races") for summary in dashboard.open_rounds)
        assert [summary.id for summary in dashboard.scored_rounds] == list(range(125, 100, -1))
        assert all(summary.submission_count == 1 for summary in dashboard.scored_rounds)
    finally:
        runtime.dispose()


def test_composed_dashboard_fails_closed_on_26th_open_round() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            _seed_dashboard_identity_and_seasons(connection)
            connection.execute(
                Win5RoundORM.__table__.insert(),
                [
                    {
                        "id": round_id,
                        "season_id": 7,
                        "type": "normal" if round_id % 2 else "special",
                        "status": "open",
                        "name": f"Open {round_id}",
                        "created_at": datetime(2026, 8, round_id),
                        "updated_at": NOW,
                    }
                    for round_id in range(1, 27)
                ],
            )

        with pytest.raises(Win5SubmissionsInvalidSourceError):
            compose_win5_submission_history_queries(runtime).get_submissions_dashboard(discord_user_id="123")
    finally:
        runtime.dispose()


def _seed_special_history(connection) -> None:
    _seed_dashboard_identity_and_seasons(connection)
    connection.execute(
        Win5RoundORM.__table__.insert(),
        {
            "id": 12,
            "season_id": 7,
            "source_kind": "imported_v1",
            "type": "special",
            "status": "scored",
            "name": "Special History",
            "created_at": NOW,
            "updated_at": NOW,
        },
    )
    connection.execute(
        Win5RaceORM.__table__.insert(),
        [_race_row(1200 + index, 12, f"Race {index}") for index in range(1, 8)],
    )
    connection.execute(
        Win5SubmissionORM.__table__.insert(),
        [
            {
                "id": 500 + index,
                "round_id": 12,
                "persona_id": "persona-1",
                "tier": "SPECIAL_WINNER",
                "status": "accepted" if index == 5 else "cancelled",
                "active_marker": True if index == 5 else None,
                "version": 2 if index < 5 else 1,
                "created_at": datetime(2026, 8, 20, index),
                "updated_at": NOW,
            }
            for index in range(6)
        ]
        + [
            {
                "id": 900,
                "round_id": 12,
                "persona_id": "persona-2",
                "tier": "SPECIAL_WINNER",
                "status": "accepted",
                "active_marker": True,
                "version": 1,
                "created_at": NOW,
                "updated_at": NOW,
            }
        ],
    )
    connection.execute(
        Win5SubmissionPickORM.__table__.insert(),
        [
            {
                "id": (500 + submission_index) * 100 + race_index,
                "submission_id": 500 + submission_index,
                "race_id": 1200 + race_index,
                "race_entry_id": None,
                "gate_number": submission_index + 1,
                "position": 1,
            }
            for submission_index in range(6)
            for race_index in range(1, 8)
            if not (submission_index == 5 and race_index == 7)
        ],
    )
    connection.execute(
        Win5RaceEntryORM.__table__.insert(),
        [
            {
                "id": (1200 + race_index) * 100 + gate,
                "race_id": 1200 + race_index,
                "gate_number": gate,
                "name": f"Race {race_index} Gate {gate}",
                "created_at": NOW,
                "updated_at": NOW,
            }
            for race_index in range(1, 8)
            for gate in range(1, 7)
        ],
    )
    winners = tuple(
        Win5SpecialResultWinner(
            id=90_000 + race_index,
            race_id=1200 + race_index,
            gate_number=6 if race_index % 2 else 1,
        )
        for race_index in range(1, 8)
    )
    connection.execute(
        Win5ResultORM.__table__.insert(),
        [
            {
                "id": winner.id,
                "race_id": winner.race_id,
                "race_entry_id": None,
                "gate_number": winner.gate_number,
                "position": 1,
                "created_at": NOW,
            }
            for winner in winners
        ],
    )
    connection.execute(
        Win5ScoreEventORM.__table__.insert(),
        {
            "id": 12_000,
            "operation_id": None,
            "season_id": 7,
            "round_id": 12,
            "race_id": None,
            "submission_id": 505,
            "submission_version": 1,
            "persona_id": "persona-1",
            "tier": "SPECIAL_WINNER",
            "result_fingerprint": fingerprint_special_result(winners),
            "scoring_policy_version": WIN5_SPECIAL_SCORING_POLICY_VERSION,
            "reward_policy_version": WIN5_SPECIAL_REWARD_POLICY_VERSION,
            "exact_count": 3,
            "wrong_position_count": 0,
            "off_board_count": 3,
            "missing_count": 1,
            "season_score_delta": 3,
            "top1_score_delta": 3,
            "circle_point_reward": 0,
            "created_at": NOW,
        },
    )
    connection.execute(
        Win5ScoreEventItemORM.__table__.insert(),
        [
            {
                "id": 13_000 + race_index,
                "score_event_id": 12_000,
                "race_id": 1200 + race_index,
                "position": 1,
                "submission_pick_id": None if race_index == 7 else 50_500 + race_index,
                "matched_result_id": 90_000 + race_index if race_index in {1, 3, 5} else None,
                "outcome": "missing" if race_index == 7 else "exact" if race_index % 2 else "off_board",
                "season_score_delta": 1 if race_index in {1, 3, 5} else 0,
            }
            for race_index in range(1, 8)
        ],
    )


def test_composed_special_history_paginates_cancelled_replacement_and_all_race_gates() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            _seed_special_history(connection)

        queries = compose_win5_submission_history_queries(runtime)
        cancelled_page = queries.get_submission_history_round(
            discord_user_id="123",
            round_id=12,
            submission_offset=0,
            race_offset=0,
        )
        replacement_page = queries.get_submission_history_round(
            discord_user_id="123",
            round_id=12,
            submission_offset=5,
            race_offset=0,
        )
        replacement_race_tail = queries.get_submission_history_round(
            discord_user_id="123",
            round_id=12,
            submission_offset=5,
            race_offset=5,
        )

        assert cancelled_page.total_submission_count == 6
        assert cancelled_page.total_race_count == 7
        assert [submission.id for submission in cancelled_page.round.submissions] == [500, 501, 502, 503, 504]
        assert all(
            submission.status == Win5SubmissionStatus.CANCELLED for submission in cancelled_page.round.submissions
        )
        assert [race.id for race in cancelled_page.round.races] == [1201, 1202, 1203, 1204, 1205]
        assert all(len(submission.picks) == 5 for submission in cancelled_page.round.submissions)

        assert [submission.id for submission in replacement_page.round.submissions] == [505]
        accepted = replacement_page.round.submissions[0]
        assert accepted.status == Win5SubmissionStatus.ACCEPTED
        assert [pick.race_id for pick in accepted.picks] == [1201, 1202, 1203, 1204, 1205]
        assert all(pick.gate_number == 6 for pick in accepted.picks)
        assert all(pick.reference_entry is not None for pick in accepted.picks)

        tail_accepted = replacement_race_tail.round.submissions[0]
        assert [race.id for race in replacement_race_tail.round.races] == [1206, 1207]
        assert [pick.race_id for pick in tail_accepted.picks] == [1206]
        assert tail_accepted.picks[0].gate_number == 6
        assert tail_accepted.picks[0].reference_entry is not None
        assert [result.race_id for result in replacement_page.round.results] == [1201, 1202, 1203, 1204, 1205]
        assert [result.race_id for result in replacement_race_tail.round.results] == [1206, 1207]
        assert accepted.judgement is not None
        assert accepted.judgement.exact_count == 3
        assert [item.outcome.value for item in accepted.judgement.items] == [
            "exact",
            "off_board",
            "exact",
            "off_board",
            "exact",
        ]
        assert tail_accepted.judgement is not None
        assert [item.outcome.value for item in tail_accepted.judgement.items] == ["off_board", "missing"]
        with pytest.raises(Win5SubmissionsUnavailableError):
            queries.get_submission_history_round(
                discord_user_id="123",
                round_id=12,
                submission_offset=6,
                race_offset=0,
            )
        with pytest.raises(Win5SubmissionsUnavailableError):
            queries.get_submission_history_round(
                discord_user_id="123",
                round_id=12,
                submission_offset=0,
                race_offset=7,
            )
    finally:
        runtime.dispose()


@pytest.mark.parametrize("corruption", ["missing_event", "missing_tail_item", "missing_tail_result"])
def test_composed_special_history_fails_closed_for_incomplete_scoring_authority(corruption: str) -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            _seed_special_history(connection)
            if corruption == "missing_event":
                connection.execute(
                    Win5ScoreEventItemORM.__table__.delete().where(Win5ScoreEventItemORM.score_event_id == 12_000)
                )
                connection.execute(Win5ScoreEventORM.__table__.delete().where(Win5ScoreEventORM.id == 12_000))
            elif corruption == "missing_tail_item":
                connection.execute(Win5ScoreEventItemORM.__table__.delete().where(Win5ScoreEventItemORM.id == 13_007))
            else:
                connection.execute(Win5ResultORM.__table__.delete().where(Win5ResultORM.id == 90_007))

        with pytest.raises(Win5SubmissionsInvalidSourceError):
            compose_win5_submission_history_queries(runtime).get_submission_history_round(
                discord_user_id="123",
                round_id=12,
                submission_offset=5,
                race_offset=0,
            )
    finally:
        runtime.dispose()


def _seed_void_aware_special_history(connection, *, cancelled: bool) -> None:
    _seed_dashboard_identity_and_seasons(connection)
    round_id = 31 if cancelled else 30
    race_ids = tuple(round_id * 100 + index for index in range(1, 4))
    submission_id = 701 if cancelled else 700
    connection.execute(
        Win5RoundORM.__table__.insert(),
        {
            "id": round_id,
            "season_id": 7,
            "type": "special",
            "status": "cancelled" if cancelled else "scored",
            "name": "All Void History" if cancelled else "Mixed Void History",
            "created_at": NOW,
            "updated_at": NOW,
        },
    )
    connection.execute(
        Win5RaceORM.__table__.insert(),
        [
            {
                **_race_row(race_id, round_id, f"Race {index}"),
                "void_reason": f"공식 취소 {index}" if cancelled or index == 2 else None,
                "voided_at": NOW if cancelled or index == 2 else None,
            }
            for index, race_id in enumerate(race_ids, start=1)
        ],
    )
    connection.execute(
        Win5SubmissionORM.__table__.insert(),
        {
            "id": submission_id,
            "round_id": round_id,
            "persona_id": "persona-1",
            "tier": "SPECIAL_WINNER",
            "status": "accepted",
            "active_marker": True,
            "version": 1,
            "created_at": NOW,
            "updated_at": NOW,
        },
    )
    pick_ids = (7_101, 7_102) if not cancelled else (7_201, 7_202)
    connection.execute(
        Win5SubmissionPickORM.__table__.insert(),
        [
            {
                "id": pick_id,
                "submission_id": submission_id,
                "race_id": race_id,
                "race_entry_id": None,
                "gate_number": gate_number,
                "position": 1,
            }
            for pick_id, race_id, gate_number in zip(
                pick_ids,
                race_ids[:2],
                (1, 9),
                strict=True,
            )
        ],
    )
    if cancelled:
        return

    winners = (
        Win5SpecialResultWinner(id=8_001, race_id=race_ids[0], gate_number=1),
        Win5SpecialResultWinner(id=8_003, race_id=race_ids[2], gate_number=3),
    )
    connection.execute(
        Win5ResultORM.__table__.insert(),
        [
            {
                "id": winner.id,
                "race_id": winner.race_id,
                "race_entry_id": None,
                "gate_number": winner.gate_number,
                "position": 1,
                "created_at": NOW,
            }
            for winner in winners
        ],
    )
    connection.execute(
        Win5ScoreEventORM.__table__.insert(),
        {
            "id": 9_000,
            "operation_id": None,
            "season_id": 7,
            "round_id": round_id,
            "race_id": None,
            "submission_id": submission_id,
            "submission_version": 1,
            "persona_id": "persona-1",
            "tier": "SPECIAL_WINNER",
            "result_fingerprint": fingerprint_special_result(winners),
            "scoring_policy_version": WIN5_SPECIAL_VOID_SCORING_POLICY_VERSION,
            "reward_policy_version": WIN5_SPECIAL_REWARD_POLICY_VERSION,
            "exact_count": 1,
            "wrong_position_count": 0,
            "off_board_count": 0,
            "missing_count": 1,
            "season_score_delta": 1,
            "top1_score_delta": 1,
            "circle_point_reward": 0,
            "created_at": NOW,
        },
    )
    connection.execute(
        Win5ScoreEventItemORM.__table__.insert(),
        [
            {
                "id": 9_101,
                "score_event_id": 9_000,
                "race_id": race_ids[0],
                "position": 1,
                "submission_pick_id": pick_ids[0],
                "matched_result_id": winners[0].id,
                "outcome": "exact",
                "season_score_delta": 1,
            },
            {
                "id": 9_102,
                "score_event_id": 9_000,
                "race_id": race_ids[1],
                "position": 1,
                "submission_pick_id": pick_ids[1],
                "matched_result_id": None,
                "outcome": "void",
                "season_score_delta": 0,
            },
            {
                "id": 9_103,
                "score_event_id": 9_000,
                "race_id": race_ids[2],
                "position": 1,
                "submission_pick_id": None,
                "matched_result_id": None,
                "outcome": "missing",
                "season_score_delta": 0,
            },
        ],
    )


def test_composed_mixed_void_special_history_projects_v2_judgement_and_non_void_results() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            _seed_void_aware_special_history(connection, cancelled=False)

        queries = compose_win5_submission_history_queries(runtime)
        dashboard = queries.get_submissions_dashboard(discord_user_id="123")
        page = queries.get_submission_history_round(
            discord_user_id="123",
            round_id=30,
            submission_offset=0,
            race_offset=0,
        )

        assert [round_.id for round_ in dashboard.scored_rounds] == [30]
        assert dashboard.cancelled_rounds == ()
        assert page.total_void_race_count == 1
        assert [race.void_reason for race in page.round.races] == [None, "공식 취소 2", None]
        assert [result.race_id for result in page.round.results] == [3001, 3003]
        submission = page.round.submissions[0]
        assert isinstance(submission.judgement, Win5SpecialSubmissionJudgement)
        assert submission.judgement.scoring_policy_version == WIN5_SPECIAL_VOID_SCORING_POLICY_VERSION
        assert submission.judgement.void_count == 1
        assert [item.outcome for item in submission.judgement.items] == [
            Win5JudgementOutcome.EXACT,
            Win5JudgementOutcome.VOID,
            Win5JudgementOutcome.MISSING,
        ]
        assert submission.judgement.items[1].submission_pick_id == 7_102
    finally:
        runtime.dispose()


def test_composed_all_void_cancelled_history_is_separate_and_score_free() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            _seed_void_aware_special_history(connection, cancelled=True)

        queries = compose_win5_submission_history_queries(runtime)
        dashboard = queries.get_submissions_dashboard(discord_user_id="123")
        page = queries.get_submission_history_round(
            discord_user_id="123",
            round_id=31,
            submission_offset=0,
            race_offset=0,
        )

        assert dashboard.scored_rounds == ()
        assert [round_.id for round_ in dashboard.cancelled_rounds] == [31]
        assert page.round.status == Win5RoundStatus.CANCELLED
        assert page.total_void_race_count == page.total_race_count == 3
        assert all(race.void_reason is not None for race in page.round.races)
        assert page.round.results == ()
        assert page.round.submissions[0].judgement is None
        assert [pick.race_id for pick in page.round.submissions[0].picks] == [3101, 3102]
    finally:
        runtime.dispose()


def test_composed_mixed_void_history_rejects_v1_policy_event() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            _seed_void_aware_special_history(connection, cancelled=False)
            connection.execute(
                Win5ScoreEventORM.__table__.update()
                .where(Win5ScoreEventORM.id == 9_000)
                .values(scoring_policy_version=WIN5_SPECIAL_SCORING_POLICY_VERSION)
            )

        with pytest.raises(Win5SubmissionsInvalidSourceError):
            compose_win5_submission_history_queries(runtime).get_submission_history_round(
                discord_user_id="123",
                round_id=30,
                submission_offset=0,
                race_offset=0,
            )
    finally:
        runtime.dispose()


@pytest.mark.parametrize("corruption", ["result", "event"])
def test_composed_all_void_cancelled_history_rejects_scoring_authority(corruption: str) -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            _seed_void_aware_special_history(connection, cancelled=True)
            if corruption == "result":
                connection.execute(
                    Win5ResultORM.__table__.insert(),
                    {
                        "id": 8_101,
                        "race_id": 3101,
                        "race_entry_id": None,
                        "gate_number": 1,
                        "position": 1,
                        "created_at": NOW,
                    },
                )
            else:
                connection.execute(
                    Win5ScoreEventORM.__table__.insert(),
                    {
                        "id": 9_100,
                        "operation_id": None,
                        "season_id": 7,
                        "round_id": 31,
                        "race_id": None,
                        "submission_id": 701,
                        "submission_version": 1,
                        "persona_id": "persona-1",
                        "tier": "SPECIAL_WINNER",
                        "result_fingerprint": "f" * 64,
                        "scoring_policy_version": WIN5_SPECIAL_VOID_SCORING_POLICY_VERSION,
                        "reward_policy_version": WIN5_SPECIAL_REWARD_POLICY_VERSION,
                        "exact_count": 0,
                        "wrong_position_count": 0,
                        "off_board_count": 0,
                        "missing_count": 0,
                        "season_score_delta": 0,
                        "top1_score_delta": 0,
                        "circle_point_reward": 0,
                        "created_at": NOW,
                    },
                )

        with pytest.raises(Win5SubmissionsInvalidSourceError):
            compose_win5_submission_history_queries(runtime).get_submission_history_round(
                discord_user_id="123",
                round_id=31,
                submission_offset=0,
                race_offset=0,
            )
    finally:
        runtime.dispose()


def _seed_normal_history(
    connection,
    *,
    event_mode: str = "valid",
    result_count: int = 5,
    round_status: str = "scored",
    submission_status: str = "accepted",
) -> None:
    _seed_dashboard_identity_and_seasons(connection)
    connection.execute(
        Win5RoundORM.__table__.insert(),
        {
            "id": 20,
            "season_id": 7,
            "type": "normal",
            "status": round_status,
            "name": "Normal History",
            "created_at": NOW,
            "updated_at": NOW,
        },
    )
    connection.execute(Win5RaceORM.__table__.insert(), _race_row(2001, 20, "Normal Final"))
    connection.execute(
        Win5RaceEntryORM.__table__.insert(),
        [
            {
                "id": 3000 + position,
                "race_id": 2001,
                "gate_number": position,
                "name": f"Horse {position}",
                "created_at": NOW,
                "updated_at": NOW,
            }
            for position in range(1, 21)
        ],
    )
    connection.execute(
        Win5SubmissionORM.__table__.insert(),
        {
            "id": 600,
            "round_id": 20,
            "persona_id": "persona-1",
            "tier": "TOP3",
            "status": submission_status,
            "active_marker": True if submission_status == "accepted" else None,
            "version": 4,
            "created_at": NOW,
            "updated_at": NOW,
        },
    )
    connection.execute(
        Win5SubmissionPickORM.__table__.insert(),
        [
            {
                "id": 6001,
                "submission_id": 600,
                "race_id": 2001,
                "race_entry_id": 3001,
                "gate_number": None,
                "position": 1,
            },
            {
                "id": 6003,
                "submission_id": 600,
                "race_id": 2001,
                "race_entry_id": 3020,
                "gate_number": None,
                "position": 3,
            },
        ],
    )
    if result_count:
        connection.execute(
            Win5ResultORM.__table__.insert(),
            [
                {
                    "id": 9000 + position,
                    "race_id": 2001,
                    "race_entry_id": 3000 + position,
                    "gate_number": None,
                    "position": position,
                    "created_at": NOW,
                }
                for position in range(1, result_count + 1)
            ],
        )
    if event_mode == "missing" or round_status != "scored":
        return

    fingerprint = fingerprint_normal_result(
        tuple(
            Win5NormalResultPlacement(
                id=9000 + position,
                position=position,
                race_entry_id=3000 + position,
            )
            for position in range(1, 6)
        )
    )
    malformed = event_mode == "malformed"
    connection.execute(
        Win5ScoreEventORM.__table__.insert(),
        {
            "id": 10001,
            "operation_id": None,
            "season_id": 7,
            "round_id": 20,
            "race_id": 2001,
            "submission_id": 600,
            "submission_version": 4,
            "persona_id": "persona-1",
            "tier": "TOP3",
            "result_fingerprint": fingerprint,
            "scoring_policy_version": WIN5_NORMAL_SCORING_POLICY_VERSION,
            "reward_policy_version": WIN5_NORMAL_REWARD_POLICY_VERSION,
            "exact_count": 0 if malformed else 1,
            "wrong_position_count": 0,
            "off_board_count": 1,
            "missing_count": 2 if malformed else 1,
            "season_score_delta": 0 if malformed else 3,
            "top1_score_delta": 0,
            "circle_point_reward": 0 if malformed else 10,
            "created_at": NOW,
        },
    )
    connection.execute(
        Win5ScoreEventItemORM.__table__.insert(),
        [
            {
                "id": 11001,
                "score_event_id": 10001,
                "race_id": 2001,
                "position": 1,
                "submission_pick_id": 6001,
                "matched_result_id": 9001,
                "outcome": "exact",
                "season_score_delta": 3,
            },
            {
                "id": 11002,
                "score_event_id": 10001,
                "race_id": 2001,
                "position": 2,
                "submission_pick_id": None,
                "matched_result_id": None,
                "outcome": "missing",
                "season_score_delta": 0,
            },
            {
                "id": 11003,
                "score_event_id": 10001,
                "race_id": 2001,
                "position": 3,
                "submission_pick_id": 6003,
                "matched_result_id": None,
                "outcome": "off_board",
                "season_score_delta": 0,
            },
        ],
    )


def test_composed_normal_history_projects_bounded_entries_results_and_missing_judgement() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            _seed_normal_history(connection)

        page = compose_win5_submission_history_queries(runtime).get_submission_history_round(
            discord_user_id="123",
            round_id=20,
            submission_offset=0,
            race_offset=0,
        )

        assert page.total_submission_count == 1
        assert page.total_race_count == 1
        assert len(page.round.races) == 1
        assert [entry.id for entry in page.round.races[0].entries] == [3001, 3002, 3003, 3004, 3005, 3020]
        assert len(page.round.results) == 5
        submission = page.round.submissions[0]
        assert [pick.race_entry_id for pick in submission.picks] == [3001, 3020]
        assert submission.judgement is not None
        assert [item.outcome.value for item in submission.judgement.score.items] == [
            "exact",
            "missing",
            "off_board",
        ]
        assert submission.judgement.score.season_score_delta == 3
        assert submission.judgement.score.circle_point_reward == 10
    finally:
        runtime.dispose()


@pytest.mark.parametrize("event_mode", ["missing", "malformed"])
def test_composed_normal_history_fails_closed_for_missing_or_malformed_event(event_mode: str) -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            _seed_normal_history(connection, event_mode=event_mode)

        with pytest.raises(Win5SubmissionsInvalidSourceError):
            compose_win5_submission_history_queries(runtime).get_submission_history_round(
                discord_user_id="123",
                round_id=20,
                submission_offset=0,
                race_offset=0,
            )
    finally:
        runtime.dispose()


@pytest.mark.parametrize("result_count", [4, 6])
def test_composed_normal_history_rejects_result_underflow_or_overflow(result_count: int) -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            _seed_normal_history(
                connection,
                event_mode="missing",
                result_count=result_count,
            )

        with pytest.raises(Win5SubmissionsInvalidSourceError):
            compose_win5_submission_history_queries(runtime).get_submission_history_round(
                discord_user_id="123",
                round_id=20,
                submission_offset=0,
                race_offset=0,
            )
    finally:
        runtime.dispose()


def test_composed_open_history_rejects_authoritative_result() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            _seed_normal_history(
                connection,
                event_mode="missing",
                result_count=1,
                round_status="open",
            )

        with pytest.raises(Win5SubmissionsInvalidSourceError):
            compose_win5_submission_history_queries(runtime).get_submission_history_round(
                discord_user_id="123",
                round_id=20,
                submission_offset=0,
                race_offset=0,
            )
    finally:
        runtime.dispose()


def test_composed_normal_history_rejects_cancelled_submission_score_event() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            _seed_normal_history(
                connection,
                submission_status="cancelled",
            )

        with pytest.raises(Win5SubmissionsInvalidSourceError):
            compose_win5_submission_history_queries(runtime).get_submission_history_round(
                discord_user_id="123",
                round_id=20,
                submission_offset=0,
                race_offset=0,
            )
    finally:
        runtime.dispose()
