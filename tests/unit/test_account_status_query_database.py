"""SQLite-backed projection tests for private Account status."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select

from uma_st2.application.execution import QueryRunner
from uma_st2.application.identity import (
    AccountEligibilityState,
    AccountStatusInvalidSourceError,
    AccountStatusQueries,
)
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyAccountStatusQueryUnitOfWorkFactory,
)
from uma_st2.infrastructure.database.orm import (
    Base,
    CirclePointORM,
    DiscordAccountORM,
    GameAccountORM,
    GameAccountRegistrationRequestORM,
    MatchEntryORM,
    MatchORM,
    PersonaORM,
    RatingORM,
    StadiumCourseORM,
    StadiumORM,
    UmamusumeORM,
    Win5RoundORM,
    Win5ScoreEventORM,
    Win5ScoreORM,
    Win5SeasonORM,
    Win5SubmissionORM,
)

NOW = datetime(2026, 9, 2, 3, 0, tzinfo=UTC)
DB_NOW = NOW.replace(tzinfo=None)


def _queries(runtime: DatabaseRuntime) -> AccountStatusQueries:
    return AccountStatusQueries(
        QueryRunner(SqlAlchemyAccountStatusQueryUnitOfWorkFactory(runtime.session_factory)),
        clock=lambda: NOW,
    )


def _seed(runtime: DatabaseRuntime) -> None:
    with runtime.engine.begin() as connection:
        connection.execute(
            PersonaORM.__table__.insert(),
            [
                {
                    "id": "persona-1",
                    "display_name": "주인공",
                    "status": "normal",
                    "created_at": DB_NOW,
                    "updated_at": DB_NOW,
                },
                {
                    "id": "persona-2",
                    "display_name": "다른 참가자",
                    "status": "normal",
                    "created_at": DB_NOW,
                    "updated_at": DB_NOW,
                },
            ],
        )
        connection.execute(
            DiscordAccountORM.__table__.insert(),
            {
                "id": 1,
                "discord_user_id": "123",
                "persona_id": "persona-1",
                "created_at": DB_NOW,
                "updated_at": DB_NOW,
            },
        )
        connection.execute(
            GameAccountORM.__table__.insert(),
            [
                {
                    "id": 1,
                    "persona_id": "persona-1",
                    "game_region": "KR",
                    "uma_pid": "123456789",
                    "nickname": "첫 계정",
                    "affiliation": "A 서클",
                    "created_at": DB_NOW,
                    "updated_at": DB_NOW,
                },
                {
                    "id": 2,
                    "persona_id": "persona-1",
                    "game_region": "JP",
                    "uma_pid": "987654321",
                    "nickname": "둘째 계정",
                    "affiliation": None,
                    "created_at": DB_NOW,
                    "updated_at": DB_NOW,
                },
                {
                    "id": 3,
                    "persona_id": "persona-2",
                    "game_region": "KR",
                    "uma_pid": "111222333",
                    "nickname": "상대 계정",
                    "affiliation": None,
                    "created_at": DB_NOW,
                    "updated_at": DB_NOW,
                },
            ],
        )
        connection.execute(
            CirclePointORM.__table__.insert(),
            [
                {"persona_id": "persona-1", "balance": 730, "updated_at": DB_NOW},
                {"persona_id": "persona-2", "balance": 500, "updated_at": DB_NOW},
            ],
        )
        connection.execute(
            RatingORM.__table__.insert(),
            [
                {"game_account_id": 1, "rating": Decimal("1500.000000000000000000"), "updated_at": DB_NOW},
                {"game_account_id": 2, "rating": Decimal("1600.000000000000000000"), "updated_at": DB_NOW},
                {"game_account_id": 3, "rating": Decimal("1550.000000000000000000"), "updated_at": DB_NOW},
            ],
        )
        connection.execute(
            StadiumORM.__table__.insert(),
            {
                "id": 1,
                "external_id": 100,
                "name_jp": "Tokyo",
                "name_ko": "도쿄",
                "created_at": DB_NOW,
                "updated_at": DB_NOW,
            },
        )
        connection.execute(
            StadiumCourseORM.__table__.insert(),
            {
                "id": 1,
                "stadium_id": 1,
                "external_id": 101,
                "surface": "turf",
                "distance": 2400,
                "direction": "left",
                "layout": "standard",
                "created_at": DB_NOW,
                "updated_at": DB_NOW,
            },
        )
        connection.execute(
            UmamusumeORM.__table__.insert(),
            [
                {
                    "id": 1,
                    "external_id": 1,
                    "name_jp": "Horse One",
                    "name_ko": "우마무스메 1",
                    "created_at": DB_NOW,
                    "updated_at": DB_NOW,
                },
                {
                    "id": 2,
                    "external_id": 2,
                    "name_jp": "Horse Two",
                    "name_ko": "우마무스메 2",
                    "created_at": DB_NOW,
                    "updated_at": DB_NOW,
                },
                {
                    "id": 3,
                    "external_id": 3,
                    "name_jp": "Horse Three",
                    "name_ko": "우마무스메 3",
                    "created_at": DB_NOW,
                    "updated_at": DB_NOW,
                },
            ],
        )
        connection.execute(
            MatchORM.__table__.insert(),
            [
                {
                    "id": 10,
                    "name": "정산 경기",
                    "description": None,
                    "source_kind": "native_v2",
                    "grade": "G1",
                    "stadium_course_id": 1,
                    "scheduled_at": datetime(2026, 8, 20, 12, 0),
                    "status": "settled",
                    "terminal_reason": None,
                    "finish_time_ms": 120000,
                    "created_at": DB_NOW,
                    "updated_at": DB_NOW,
                },
                {
                    "id": 11,
                    "name": "취소 경기",
                    "description": None,
                    "source_kind": "native_v2",
                    "grade": "G2",
                    "stadium_course_id": 1,
                    "scheduled_at": datetime(2026, 8, 25, 12, 0),
                    "status": "cancelled",
                    "terminal_reason": "운영 취소",
                    "finish_time_ms": None,
                    "created_at": DB_NOW,
                    "updated_at": DB_NOW,
                },
                {
                    "id": 12,
                    "name": "지난 반기",
                    "description": None,
                    "source_kind": "imported_v1",
                    "grade": "G3",
                    "stadium_course_id": 1,
                    "scheduled_at": datetime(2026, 5, 20, 12, 0),
                    "status": "result_confirmed",
                    "terminal_reason": None,
                    "finish_time_ms": 121000,
                    "created_at": DB_NOW,
                    "updated_at": DB_NOW,
                },
            ],
        )
        connection.execute(
            MatchEntryORM.__table__.insert(),
            [
                {
                    "id": 101,
                    "match_id": 10,
                    "game_account_id": 1,
                    "owner_at_event_persona_id": "persona-1",
                    "affiliation_at_event": "A 서클",
                    "umamusume_id": 1,
                    "umamusume_variant_id": None,
                    "entry_number": 1,
                    "rank": 1,
                    "popularity_rank": 1,
                    "created_at": DB_NOW,
                    "updated_at": DB_NOW,
                },
                {
                    "id": 102,
                    "match_id": 10,
                    "game_account_id": 2,
                    "owner_at_event_persona_id": "persona-1",
                    "affiliation_at_event": None,
                    "umamusume_id": 2,
                    "umamusume_variant_id": None,
                    "entry_number": 2,
                    "rank": 3,
                    "popularity_rank": 2,
                    "created_at": DB_NOW,
                    "updated_at": DB_NOW,
                },
                {
                    "id": 103,
                    "match_id": 10,
                    "game_account_id": 3,
                    "owner_at_event_persona_id": "persona-2",
                    "affiliation_at_event": None,
                    "umamusume_id": 3,
                    "umamusume_variant_id": None,
                    "entry_number": 3,
                    "rank": 2,
                    "popularity_rank": 3,
                    "created_at": DB_NOW,
                    "updated_at": DB_NOW,
                },
                {
                    "id": 104,
                    "match_id": 11,
                    "game_account_id": 1,
                    "owner_at_event_persona_id": "persona-1",
                    "affiliation_at_event": "A 서클",
                    "umamusume_id": 1,
                    "umamusume_variant_id": None,
                    "entry_number": 1,
                    "rank": None,
                    "popularity_rank": None,
                    "created_at": DB_NOW,
                    "updated_at": DB_NOW,
                },
                {
                    "id": 105,
                    "match_id": 12,
                    "game_account_id": 1,
                    "owner_at_event_persona_id": "persona-1",
                    "affiliation_at_event": "A 서클",
                    "umamusume_id": 1,
                    "umamusume_variant_id": None,
                    "entry_number": 1,
                    "rank": 1,
                    "popularity_rank": 1,
                    "created_at": DB_NOW,
                    "updated_at": DB_NOW,
                },
            ],
        )
        connection.execute(
            Win5SeasonORM.__table__.insert(),
            {
                "id": 20,
                "name": "2026 하반기",
                "status": "active",
                "active_marker": True,
                "starts_at": datetime(2026, 7, 1),
                "ends_at": None,
                "created_at": DB_NOW,
                "updated_at": DB_NOW,
            },
        )
        connection.execute(
            Win5RoundORM.__table__.insert(),
            [
                {
                    "id": 201,
                    "season_id": 20,
                    "source_kind": "native_v2",
                    "type": "normal",
                    "status": "scored",
                    "name": "채점 라운드",
                    "opens_at": None,
                    "closes_at": None,
                    "created_at": DB_NOW,
                    "updated_at": datetime(2026, 8, 20),
                },
                {
                    "id": 202,
                    "season_id": 20,
                    "source_kind": "native_v2",
                    "type": "special",
                    "status": "open",
                    "name": "접수 라운드",
                    "opens_at": None,
                    "closes_at": None,
                    "created_at": DB_NOW,
                    "updated_at": datetime(2026, 8, 25),
                },
            ],
        )
        connection.execute(
            Win5SubmissionORM.__table__.insert(),
            [
                {
                    "id": 301,
                    "round_id": 201,
                    "persona_id": "persona-1",
                    "tier": "TOP1",
                    "status": "accepted",
                    "active_marker": True,
                    "version": 1,
                    "created_at": datetime(2026, 8, 20),
                    "updated_at": datetime(2026, 8, 20),
                },
                {
                    "id": 302,
                    "round_id": 202,
                    "persona_id": "persona-1",
                    "tier": "SPECIAL_WINNER",
                    "status": "accepted",
                    "active_marker": True,
                    "version": 1,
                    "created_at": datetime(2026, 8, 25),
                    "updated_at": datetime(2026, 8, 25),
                },
            ],
        )
        connection.execute(
            Win5ScoreORM.__table__.insert(),
            [
                {
                    "season_id": 20,
                    "persona_id": "persona-1",
                    "season_score": 20,
                    "top1_score": 5,
                    "updated_at": DB_NOW,
                },
                {
                    "season_id": 20,
                    "persona_id": "persona-2",
                    "season_score": 10,
                    "top1_score": 2,
                    "updated_at": DB_NOW,
                },
            ],
        )
        connection.execute(
            Win5ScoreEventORM.__table__.insert(),
            {
                "id": 401,
                "operation_id": None,
                "season_id": 20,
                "round_id": 201,
                "race_id": None,
                "submission_id": 301,
                "submission_version": 1,
                "persona_id": "persona-1",
                "tier": "TOP1",
                "result_fingerprint": "a" * 64,
                "scoring_policy_version": "test-scoring-v1",
                "reward_policy_version": "test-reward-v1",
                "exact_count": 1,
                "wrong_position_count": 0,
                "off_board_count": 0,
                "missing_count": 0,
                "season_score_delta": 5,
                "top1_score_delta": 5,
                "circle_point_reward": 10,
                "created_at": datetime(2026, 8, 20),
            },
        )
        connection.execute(
            GameAccountRegistrationRequestORM.__table__.insert(),
            [
                {
                    "id": 501,
                    "guild_id": "987",
                    "requester_discord_user_id": "123",
                    "discord_display_name_snapshot": "표시명",
                    "game_region": "KR",
                    "uma_pid": "123456789",
                    "nickname": "첫 계정",
                    "affiliation": "소속",
                    "status": "approved",
                    "active_marker": None,
                    "reason": None,
                    "created_at": datetime(2026, 8, 1),
                    "resolved_at": datetime(2026, 8, 2),
                },
                {
                    "id": 502,
                    "guild_id": "987",
                    "requester_discord_user_id": "999",
                    "discord_display_name_snapshot": "승인 대기자",
                    "game_region": "JP",
                    "uma_pid": "555566667777",
                    "nickname": "승인 대기 계정",
                    "affiliation": None,
                    "status": "pending",
                    "active_marker": True,
                    "reason": None,
                    "created_at": datetime(2026, 9, 1),
                    "resolved_at": None,
                },
            ],
        )


def test_account_status_projection_preserves_identity_match_rating_point_and_win5_boundaries() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        _seed(runtime)
        queries = _queries(runtime)
        with runtime.engine.connect() as connection:
            before = connection.scalar(select(func.count()).select_from(PersonaORM))

        overview = queries.get_overview(discord_user_id="123", guild_id="987")
        assert overview.display_name == "주인공"
        assert overview.eligibility is AccountEligibilityState.ELIGIBLE
        assert overview.wallet_balance == 730
        assert overview.game_account_count == overview.eligible_game_account_count == 2
        assert overview.match.participated_match_count == 1
        assert overview.match.entry_count == 2
        assert overview.match.win_count == 1
        assert overview.match.top3_count == 2
        assert overview.match.average_rank == Decimal("2")
        assert overview.match.excluded_terminal_match_count == 1
        assert overview.win5 is not None
        assert overview.win5.season_score == 20
        assert overview.win5.competition_rank == 1
        assert overview.win5.submitted_round_count == 2
        assert overview.win5.scored_round_count == 1

        match_page = queries.get_match_history(discord_user_id="123")
        assert match_page.total_count == 3
        assert [item.match_id for item in match_page.items] == [11, 10, 10]
        assert match_page.items[0].rank is None
        assert match_page.items[1].field_size == 3
        assert match_page.items[1].character_name == "우마무스메 1"
        assert all(item.match_id != 12 for item in match_page.items)

        win5_page = queries.get_win5_history(discord_user_id="123")
        assert win5_page.total_count == 2
        assert [item.round_id for item in win5_page.items] == [202, 201]
        assert win5_page.items[0].score_delta is None
        assert win5_page.items[1].score_delta == 5

        identity = queries.get_identity_details(discord_user_id="123", guild_id="987")
        assert identity.total_count == 2
        assert [account.pid_hint for account in identity.accounts] == ["••••6789", "••••4321"]
        assert [account.competition_rank for account in identity.accounts] == [3, 1]
        assert identity.registration_request is not None
        assert identity.registration_request.pid_hint == "••••6789"

        with runtime.engine.connect() as connection:
            after = connection.scalar(select(func.count()).select_from(PersonaORM))
        assert before == after == 2
    finally:
        runtime.dispose()


def test_unlinked_requester_gets_private_registration_state_without_canonical_identity() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        _seed(runtime)
        queries = _queries(runtime)

        overview = queries.get_overview(discord_user_id="999", guild_id="987")
        identity = queries.get_identity_details(discord_user_id="999", guild_id="987")

        assert overview.persona_id is None
        assert overview.eligibility is AccountEligibilityState.REGISTRATION_PENDING
        assert overview.registration_request is not None
        assert overview.registration_request.pid_hint == "••••7777"
        assert identity.persona_id is None
        assert identity.accounts == ()
        assert identity.registration_request is not None
    finally:
        runtime.dispose()


def test_account_status_fails_closed_on_inconsistent_active_season_marker() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        _seed(runtime)
        with runtime.engine.begin() as connection:
            connection.execute(
                Win5SeasonORM.__table__.update().where(Win5SeasonORM.id == 20).values(active_marker=None)
            )

        with pytest.raises(AccountStatusInvalidSourceError, match="malformed"):
            _queries(runtime).get_overview(discord_user_id="123", guild_id="987")
    finally:
        runtime.dispose()
