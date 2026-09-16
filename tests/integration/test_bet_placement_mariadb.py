"""MariaDB atomic native member Match Bet placement and replacement evidence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Engine

from uma_st2.application.betting import (
    BET_REPLACEMENT_REFUND_POINT_ACTION,
    BET_STAKE_POINT_ACTION,
    BetPlacementCommands,
    BetPlacementDuplicateError,
    BetPlacementIdentityError,
    BetPlacementInsufficientBalanceError,
    BetPlacementStakeLimitError,
    BetPlacementUnavailableError,
    BetPlacementWalletUnavailableError,
    BetReplacementCommands,
    BetReplacementDuplicateError,
    BetReplacementIdempotencyConflictError,
    BetReplacementNoChangeError,
    BetReplacementStakeLimitError,
    BetReplacementUnavailableError,
    MatchMemberBettingQueries,
    MatchRaceDetailUnavailableError,
    PlaceMatchBet,
    ReplaceMatchBet,
)
from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.match import CloseMatchBetting, MatchBettingCloseCommands
from uma_st2.domain.betting import BetStatus, BetType
from uma_st2.domain.match import (
    MatchDirection,
    MatchGrade,
    MatchSeason,
    MatchStatus,
    MatchSurface,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
    StadiumCourseLayout,
)
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyBetPlacementUnitOfWorkFactory,
    SqlAlchemyBetReplacementUnitOfWorkFactory,
    SqlAlchemyMatchBettingCloseUnitOfWorkFactory,
    SqlAlchemyMatchMemberBettingQueryUnitOfWorkFactory,
)
from uma_st2.infrastructure.database.orm import (
    BetOperationORM,
    BetORM,
    CirclePointORM,
    DiscordAccountORM,
    DiscordPublicationORM,
    GameAccountORM,
    MatchConditionORM,
    MatchEntryORM,
    MatchOperationORM,
    MatchORM,
    OperationORM,
    PersonaORM,
    PointTransactionORM,
    StadiumCourseORM,
    StadiumORM,
    UmamusumeORM,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class SeededBetMatch:
    match_id: int
    guild_id: str
    discord_user_id: str
    bettor_persona_id: str
    stadium_id: int
    course_id: int
    persona_ids: tuple[str, ...]
    account_ids: tuple[int, ...]
    umamusume_ids: tuple[int, ...]
    entry_ids: tuple[int, ...]


def _seed_bet_match(engine: Engine, *, suffix: str, balance: int = 500) -> SeededBetMatch:
    stored_now = (NOW - timedelta(days=1)).replace(tzinfo=None)
    external_base = int(suffix[:12], 16)
    guild_id = str(int(suffix[:15], 16) + 1)
    discord_user_id = str(int(suffix[15:30], 16) + 1)
    persona_ids = tuple(str(uuid4()) for _ in range(3))
    with engine.begin() as connection:
        connection.execute(
            PersonaORM.__table__.insert(),
            [
                {
                    "id": persona_id,
                    "display_name": f"Persona {index} {suffix}",
                    "status": "normal",
                    "created_at": stored_now,
                    "updated_at": stored_now,
                }
                for index, persona_id in enumerate(persona_ids, start=1)
            ],
        )
        account_ids = tuple(
            connection.execute(
                GameAccountORM.__table__.insert().values(
                    persona_id=persona_id,
                    game_region="KR",
                    uma_pid=f"{index}{suffix[:15]}",
                    nickname=f"Account {index} {suffix}",
                    affiliation="A조",
                    created_at=stored_now,
                    updated_at=stored_now,
                )
            ).inserted_primary_key[0]
            for index, persona_id in enumerate(persona_ids, start=1)
        )
        connection.execute(
            DiscordAccountORM.__table__.insert().values(
                discord_user_id=discord_user_id,
                persona_id=persona_ids[0],
                created_at=stored_now,
                updated_at=stored_now,
            )
        )
        connection.execute(
            CirclePointORM.__table__.insert().values(
                persona_id=persona_ids[0],
                balance=balance,
                updated_at=stored_now,
            )
        )
        umamusume_ids = tuple(
            connection.execute(
                UmamusumeORM.__table__.insert().values(
                    external_id=external_base + index,
                    name_jp=f"Horse JP {index} {suffix}",
                    name_ko=f"말 {index} {suffix}",
                    created_at=stored_now,
                    updated_at=stored_now,
                )
            ).inserted_primary_key[0]
            for index in range(1, 4)
        )
        stadium_id = connection.execute(
            StadiumORM.__table__.insert().values(
                external_id=external_base + 100,
                name_jp=f"Tokyo {suffix}",
                name_ko=f"도쿄 {suffix}",
                created_at=stored_now,
                updated_at=stored_now,
            )
        ).inserted_primary_key[0]
        course_id = connection.execute(
            StadiumCourseORM.__table__.insert().values(
                stadium_id=stadium_id,
                external_id=external_base + 101,
                surface="turf",
                distance=2400,
                direction="left",
                layout="standard",
                created_at=stored_now,
                updated_at=stored_now,
            )
        ).inserted_primary_key[0]
        match_id = connection.execute(
            MatchORM.__table__.insert().values(
                name=f"Bet Match {suffix}",
                description="공식 룸매치",
                source_kind="native_v2",
                grade="G1",
                stadium_course_id=course_id,
                scheduled_at=SCHEDULED_AT.replace(tzinfo=None),
                status="betting_open",
                terminal_reason=None,
                finish_time_ms=None,
                created_at=stored_now,
                updated_at=stored_now,
            )
        ).inserted_primary_key[0]
        connection.execute(
            MatchConditionORM.__table__.insert().values(
                match_id=match_id,
                season="spring",
                weather="cloudy",
                time_of_day="night",
                track_condition="good",
                created_at=stored_now,
                updated_at=stored_now,
            )
        )
        entry_ids = tuple(
            connection.execute(
                MatchEntryORM.__table__.insert().values(
                    match_id=match_id,
                    game_account_id=account_id,
                    owner_at_event_persona_id=persona_id,
                    affiliation_at_event="A조",
                    umamusume_id=umamusume_id,
                    umamusume_variant_id=None,
                    entry_number=index,
                    running_style=None,
                    training_grade=None,
                    rank=None,
                    popularity_rank=None,
                    margin=None,
                    created_at=stored_now,
                    updated_at=stored_now,
                )
            ).inserted_primary_key[0]
            for index, (account_id, persona_id, umamusume_id) in enumerate(
                zip(account_ids, persona_ids, umamusume_ids, strict=True),
                start=1,
            )
        )
    return SeededBetMatch(
        match_id=match_id,
        guild_id=guild_id,
        discord_user_id=discord_user_id,
        bettor_persona_id=persona_ids[0],
        stadium_id=stadium_id,
        course_id=course_id,
        persona_ids=persona_ids,
        account_ids=account_ids,
        umamusume_ids=umamusume_ids,
        entry_ids=entry_ids,
    )


def _services(
    engine: Engine,
) -> tuple[BetPlacementCommands, MatchMemberBettingQueries, MatchBettingCloseCommands]:
    runtime = DatabaseRuntime.from_engine(engine)
    return (
        BetPlacementCommands(
            CommandRunner(SqlAlchemyBetPlacementUnitOfWorkFactory(runtime.session_factory)),
            clock=lambda: NOW,
        ),
        MatchMemberBettingQueries(
            QueryRunner(SqlAlchemyMatchMemberBettingQueryUnitOfWorkFactory(runtime.session_factory))
        ),
        MatchBettingCloseCommands(
            CommandRunner(SqlAlchemyMatchBettingCloseUnitOfWorkFactory(runtime.session_factory)),
            clock=lambda: NOW,
        ),
    )


def _request(
    seeded: SeededBetMatch,
    *,
    key: str,
    amount: int = 20,
) -> PlaceMatchBet:
    return PlaceMatchBet(
        match_id=seeded.match_id,
        bet_type=BetType.QUINELLA,
        entry_numbers=(2, 1),
        amount=amount,
        actor_discord_user_id=seeded.discord_user_id,
        guild_id=seeded.guild_id,
        idempotency_key=key,
        correlation_id=key,
    )


def _close_request(seeded: SeededBetMatch, *, key: str) -> CloseMatchBetting:
    return CloseMatchBetting(
        match_id=seeded.match_id,
        idempotency_key=key,
        actor_discord_user_id="operator-1",
        guild_id=seeded.guild_id,
        correlation_id=key,
    )


def _replacement_commands(engine: Engine) -> BetReplacementCommands:
    runtime = DatabaseRuntime.from_engine(engine)
    return BetReplacementCommands(
        CommandRunner(SqlAlchemyBetReplacementUnitOfWorkFactory(runtime.session_factory)),
        clock=lambda: NOW,
    )


def _replacement_request(
    seeded: SeededBetMatch,
    *,
    bet_id: int,
    key: str,
    bet_type: BetType = BetType.QUINELLA,
    entry_numbers: tuple[int, ...] = (3, 1),
    amount: int = 30,
) -> ReplaceMatchBet:
    return ReplaceMatchBet(
        bet_id=bet_id,
        bet_type=bet_type,
        entry_numbers=entry_numbers,
        amount=amount,
        actor_discord_user_id=seeded.discord_user_id,
        guild_id=seeded.guild_id,
        idempotency_key=key,
        correlation_id=key,
    )


def _cleanup(engine: Engine, seeded: SeededBetMatch) -> None:
    with engine.begin() as connection:
        operation_ids = tuple(
            connection.scalars(
                select(OperationORM.id).where(
                    OperationORM.id.in_(
                        select(BetOperationORM.operation_id).where(BetOperationORM.match_id == seeded.match_id)
                    )
                )
            )
        ) + tuple(
            connection.scalars(
                select(MatchOperationORM.operation_id).where(MatchOperationORM.match_id == seeded.match_id)
            )
        )
        if operation_ids:
            connection.execute(delete(PointTransactionORM).where(PointTransactionORM.operation_id.in_(operation_ids)))
            connection.execute(delete(BetOperationORM).where(BetOperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(MatchOperationORM).where(MatchOperationORM.operation_id.in_(operation_ids)))
        connection.execute(
            delete(DiscordPublicationORM).where(
                DiscordPublicationORM.source_kind == "match",
                DiscordPublicationORM.source_id == seeded.match_id,
            )
        )
        connection.execute(delete(BetORM).where(BetORM.match_id == seeded.match_id))
        if operation_ids:
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(delete(CirclePointORM).where(CirclePointORM.persona_id == seeded.bettor_persona_id))
        connection.execute(delete(DiscordAccountORM).where(DiscordAccountORM.discord_user_id == seeded.discord_user_id))
        connection.execute(delete(MatchEntryORM).where(MatchEntryORM.match_id == seeded.match_id))
        connection.execute(delete(MatchConditionORM).where(MatchConditionORM.match_id == seeded.match_id))
        connection.execute(delete(MatchORM).where(MatchORM.id == seeded.match_id))
        connection.execute(delete(StadiumCourseORM).where(StadiumCourseORM.id == seeded.course_id))
        connection.execute(delete(StadiumORM).where(StadiumORM.id == seeded.stadium_id))
        connection.execute(delete(UmamusumeORM).where(UmamusumeORM.id.in_(seeded.umamusume_ids)))
        connection.execute(delete(GameAccountORM).where(GameAccountORM.id.in_(seeded.account_ids)))
        connection.execute(delete(PersonaORM).where(PersonaORM.id.in_(seeded.persona_ids)))


def test_placement_debits_wallet_and_records_exact_evidence_once(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_bet_match(migrated_engine, suffix=suffix)
    stored_now = (NOW - timedelta(days=1)).replace(tzinfo=None)
    with migrated_engine.begin() as connection:
        scheduled_match_id = connection.execute(
            MatchORM.__table__.insert().values(
                name=f"Scheduled Match {suffix}",
                description=None,
                source_kind="native_v2",
                grade="OP",
                stadium_course_id=seeded.course_id,
                scheduled_at=(SCHEDULED_AT + timedelta(hours=1)).replace(tzinfo=None),
                status="scheduled",
                terminal_reason=None,
                finish_time_ms=None,
                created_at=stored_now,
                updated_at=stored_now,
            )
        ).inserted_primary_key[0]
        connection.execute(
            MatchConditionORM.__table__.insert().values(
                match_id=scheduled_match_id,
                season="autumn",
                weather="sunny",
                time_of_day="day",
                track_condition="firm",
                created_at=stored_now,
                updated_at=stored_now,
            )
        )
    commands, queries, close_commands = _services(migrated_engine)
    request = _request(seeded, key=f"bet-{suffix}")
    try:
        choices = queries.search_targets(search=suffix, limit=25)
        assert [(choice.match_id, choice.entry_count) for choice in choices] == [(seeded.match_id, 3)]
        assert choices[0].grade == MatchGrade.G1
        listed = queries.list_races()
        assert [(choice.match_id, choice.grade, choice.status, choice.entry_count) for choice in listed] == [
            (seeded.match_id, MatchGrade.G1, MatchStatus.BETTING_OPEN, 3),
            (scheduled_match_id, MatchGrade.OP, MatchStatus.SCHEDULED, 0),
        ]
        searched = queries.search_races(search=f"Scheduled Match {suffix}", limit=25)
        assert [(choice.match_id, choice.status, choice.entry_count) for choice in searched] == [
            (scheduled_match_id, MatchStatus.SCHEDULED, 0)
        ]
        detail = queries.get_race_detail(match_id=seeded.match_id)
        assert (detail.match_id, detail.description, detail.grade, detail.status) == (
            seeded.match_id,
            "공식 룸매치",
            MatchGrade.G1,
            MatchStatus.BETTING_OPEN,
        )
        assert (
            detail.course.stadium_name,
            detail.course.surface,
            detail.course.distance,
            detail.course.direction,
            detail.course.layout,
        ) == (
            f"도쿄 {suffix}",
            MatchSurface.TURF,
            2400,
            MatchDirection.LEFT,
            StadiumCourseLayout.STANDARD,
        )
        assert (
            detail.condition.season,
            detail.condition.weather,
            detail.condition.time_of_day,
            detail.condition.track_condition,
        ) == (
            MatchSeason.SPRING,
            MatchWeather.CLOUDY,
            MatchTimeOfDay.NIGHT,
            MatchTrackCondition.GOOD,
        )
        assert [
            (
                entry.entry_number,
                entry.game_account_name,
                entry.umamusume_name,
                entry.affiliation,
            )
            for entry in detail.entries
        ] == [(index, f"Account {index} {suffix}", f"말 {index} {suffix}", "A조") for index in range(1, 4)]
        scheduled_detail = queries.get_race_detail(match_id=scheduled_match_id)
        assert scheduled_detail.status == MatchStatus.SCHEDULED
        assert scheduled_detail.entries == ()
        assert queries.list_personal_bets(actor_discord_user_id=f"missing-{suffix[:20]}") == ()
        point_state = queries.get_input_point_state(actor_discord_user_id=seeded.discord_user_id)
        assert point_state is not None
        assert (point_state.balance, point_state.maximum_stake) == (500, 50)

        placed = commands.place_bet(request)
        point_state = queries.get_input_point_state(actor_discord_user_id=seeded.discord_user_id)
        assert point_state is not None
        assert (point_state.balance, point_state.maximum_stake) == (480, 40)
        with pytest.raises(BetPlacementDuplicateError):
            commands.place_bet(_request(seeded, key=f"duplicate-{suffix}", amount=30))
        closed = close_commands.close_betting(_close_request(seeded, key=f"close-after-bet-{suffix}"))
        assert closed.active_bet_count == 1
        with pytest.raises(MatchRaceDetailUnavailableError):
            queries.get_race_detail(match_id=seeded.match_id)
        assert commands.place_bet(request) == placed
        history = queries.list_personal_bets(actor_discord_user_id=seeded.discord_user_id)
        assert [
            (
                item.bet_id,
                item.entry_numbers,
                item.amount,
                item.bet_status,
                item.match_status,
            )
            for item in history
        ] == [
            (
                placed.bet_id,
                (1, 2),
                20,
                BetStatus.ACTIVE,
                MatchStatus.BETTING_CLOSED,
            )
        ]

        with migrated_engine.connect() as connection:
            bet = connection.execute(
                select(
                    BetORM.id,
                    BetORM.persona_id,
                    BetORM.type,
                    BetORM.selections,
                    BetORM.amount,
                    BetORM.status,
                    BetORM.active_marker,
                ).where(BetORM.match_id == seeded.match_id)
            ).one()
            wallet_balance = connection.scalar(
                select(CirclePointORM.balance).where(CirclePointORM.persona_id == seeded.bettor_persona_id)
            )
            point_rows = tuple(
                connection.execute(
                    select(PointTransactionORM.action, PointTransactionORM.amount)
                    .join(OperationORM, OperationORM.id == PointTransactionORM.operation_id)
                    .where(OperationORM.idempotency_key == request.idempotency_key)
                )
            )
            audit = connection.execute(
                select(
                    OperationORM.request_fingerprint,
                    BetOperationORM.type,
                    BetOperationORM.before_data,
                    BetOperationORM.after_data,
                )
                .join(BetOperationORM, BetOperationORM.operation_id == OperationORM.id)
                .where(OperationORM.idempotency_key == request.idempotency_key)
            ).one()

        assert bet.id == placed.bet_id
        assert bet.persona_id == seeded.bettor_persona_id
        assert bet.type == "quinella"
        assert bet.selections == sorted(seeded.entry_ids[:2])
        assert (bet.amount, bet.status, bet.active_marker) == (20, "active", True)
        assert wallet_balance == 480
        assert point_rows == ((BET_STAKE_POINT_ACTION, -20),)
        assert audit.request_fingerprint == request.request_fingerprint
        assert audit.type == "bet_placed"
        assert audit.before_data["balance_before"] == 500
        assert audit.after_data["balance_after"] == 480

        with migrated_engine.connect() as connection:
            assert connection.scalar(select(func.count(BetORM.id)).where(BetORM.match_id == seeded.match_id)) == 1
    finally:
        with migrated_engine.begin() as connection:
            connection.execute(delete(MatchConditionORM).where(MatchConditionORM.match_id == scheduled_match_id))
            connection.execute(delete(MatchORM).where(MatchORM.id == scheduled_match_id))
        _cleanup(migrated_engine, seeded)


def test_concurrent_exact_retry_converges_to_one_bet_and_debit(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_bet_match(migrated_engine, suffix=suffix)
    request = _request(seeded, key=f"concurrent-bet-{suffix}")
    start = Barrier(2)

    def run() -> int:
        commands, _, _ = _services(migrated_engine)
        start.wait()
        return commands.place_bet(request).bet_id

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            bet_ids = tuple(future.result() for future in (executor.submit(run), executor.submit(run)))

        assert bet_ids[0] == bet_ids[1]
        with migrated_engine.connect() as connection:
            assert connection.scalar(select(func.count(BetORM.id)).where(BetORM.match_id == seeded.match_id)) == 1
            assert (
                connection.scalar(
                    select(func.count(PointTransactionORM.id)).where(
                        PointTransactionORM.persona_id == seeded.bettor_persona_id
                    )
                )
                == 1
            )
            assert (
                connection.scalar(
                    select(CirclePointORM.balance).where(CirclePointORM.persona_id == seeded.bettor_persona_id)
                )
                == 480
            )
    finally:
        _cleanup(migrated_engine, seeded)


@pytest.mark.parametrize(
    ("failure", "expected_error", "balance", "amount"),
    (
        ("missing_wallet", BetPlacementWalletUnavailableError, 500, 20),
        ("missing_pid", BetPlacementIdentityError, 500, 20),
        ("insufficient", BetPlacementInsufficientBalanceError, 0, 10),
        ("stake_limit", BetPlacementStakeLimitError, 500, 60),
    ),
)
def test_identity_and_wallet_failures_leave_no_partial_evidence(
    migrated_engine: Engine,
    failure: str,
    expected_error: type[Exception],
    balance: int,
    amount: int,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_bet_match(
        migrated_engine,
        suffix=suffix,
        balance=balance,
    )
    request = _request(seeded, key=f"fail-{failure}-{suffix}", amount=amount)
    with migrated_engine.begin() as connection:
        if failure == "missing_wallet":
            connection.execute(delete(CirclePointORM).where(CirclePointORM.persona_id == seeded.bettor_persona_id))
        elif failure == "missing_pid":
            connection.execute(
                update(GameAccountORM).where(GameAccountORM.id == seeded.account_ids[0]).values(uma_pid=None)
            )
    commands, _, _ = _services(migrated_engine)
    try:
        with pytest.raises(expected_error):
            commands.place_bet(request)

        with migrated_engine.connect() as connection:
            assert connection.scalar(select(func.count(BetORM.id)).where(BetORM.match_id == seeded.match_id)) == 0
            assert (
                connection.scalar(
                    select(func.count(OperationORM.id)).where(OperationORM.idempotency_key == request.idempotency_key)
                )
                == 0
            )
            assert (
                connection.scalar(
                    select(func.count(PointTransactionORM.id)).where(
                        PointTransactionORM.persona_id == seeded.bettor_persona_id
                    )
                )
                == 0
            )
    finally:
        _cleanup(migrated_engine, seeded)


def test_concurrent_placement_and_close_serialize_at_match_root(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_bet_match(migrated_engine, suffix=suffix)
    start = Barrier(2)

    def place() -> bool:
        commands, _, _ = _services(migrated_engine)
        start.wait()
        try:
            commands.place_bet(_request(seeded, key=f"race-bet-{suffix}"))
        except BetPlacementUnavailableError:
            return False
        return True

    def close() -> int:
        _, _, commands = _services(migrated_engine)
        start.wait()
        return commands.close_betting(_close_request(seeded, key=f"race-close-{suffix}")).active_bet_count

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            placed_future = executor.submit(place)
            close_future = executor.submit(close)
            placed = placed_future.result()
            close_count = close_future.result()

        assert close_count == (1 if placed else 0)
        with migrated_engine.connect() as connection:
            assert connection.scalar(select(MatchORM.status).where(MatchORM.id == seeded.match_id)) == "betting_closed"
            assert (
                connection.scalar(select(func.count(BetORM.id)).where(BetORM.match_id == seeded.match_id))
                == close_count
            )
            assert connection.scalar(
                select(CirclePointORM.balance).where(CirclePointORM.persona_id == seeded.bettor_persona_id)
            ) == 500 - (20 if placed else 0)
    finally:
        _cleanup(migrated_engine, seeded)


def test_replacement_refunds_cancels_creates_and_debits_exactly_once(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_bet_match(migrated_engine, suffix=suffix)
    placement_commands, queries, close_commands = _services(migrated_engine)
    replacement_commands = _replacement_commands(migrated_engine)
    placement = placement_commands.place_bet(_request(seeded, key=f"before-replace-{suffix}"))
    request = _replacement_request(
        seeded,
        bet_id=placement.bet_id,
        key=f"replace-{suffix}",
        amount=50,
    )
    try:
        before_choices = queries.search_active_bets(
            actor_discord_user_id=seeded.discord_user_id,
            search=suffix,
            limit=25,
        )
        assert [(choice.bet_id, choice.entry_numbers, choice.amount) for choice in before_choices] == [
            (placement.bet_id, (1, 2), 20)
        ]

        replaced = replacement_commands.replace_bet(request)
        after_choices = queries.search_active_bets(
            actor_discord_user_id=seeded.discord_user_id,
            search=suffix,
            limit=25,
        )
        assert [(choice.bet_id, choice.entry_numbers, choice.amount) for choice in after_choices] == [
            (replaced.new_bet.bet_id, (1, 3), 50)
        ]
        closed = close_commands.close_betting(_close_request(seeded, key=f"close-after-replace-{suffix}"))
        assert closed.active_bet_count == 1
        history = queries.list_personal_bets(actor_discord_user_id=seeded.discord_user_id)
        assert [(item.bet_id, item.bet_status, item.match_status) for item in history] == [
            (replaced.new_bet.bet_id, BetStatus.ACTIVE, MatchStatus.BETTING_CLOSED),
            (placement.bet_id, BetStatus.CANCELLED, MatchStatus.BETTING_CLOSED),
        ]
        assert replacement_commands.replace_bet(request) == replaced
        with pytest.raises(BetReplacementIdempotencyConflictError):
            replacement_commands.replace_bet(
                _replacement_request(
                    seeded,
                    bet_id=placement.bet_id,
                    key=request.idempotency_key,
                    amount=40,
                )
            )

        with migrated_engine.connect() as connection:
            bets = tuple(
                connection.execute(
                    select(
                        BetORM.id,
                        BetORM.selections,
                        BetORM.amount,
                        BetORM.status,
                        BetORM.active_marker,
                    )
                    .where(BetORM.match_id == seeded.match_id)
                    .order_by(BetORM.id)
                )
            )
            point_rows = tuple(
                connection.execute(
                    select(PointTransactionORM.action, PointTransactionORM.amount)
                    .join(OperationORM, OperationORM.id == PointTransactionORM.operation_id)
                    .where(OperationORM.idempotency_key == request.idempotency_key)
                    .order_by(PointTransactionORM.id)
                )
            )
            audit = connection.execute(
                select(
                    OperationORM.request_fingerprint,
                    BetOperationORM.bet_id,
                    BetOperationORM.type,
                    BetOperationORM.before_data,
                    BetOperationORM.after_data,
                )
                .join(BetOperationORM, BetOperationORM.operation_id == OperationORM.id)
                .where(OperationORM.idempotency_key == request.idempotency_key)
            ).one()
            wallet_balance = connection.scalar(
                select(CirclePointORM.balance).where(CirclePointORM.persona_id == seeded.bettor_persona_id)
            )

        assert [(row.id, row.amount, row.status, row.active_marker) for row in bets] == [
            (placement.bet_id, 20, "cancelled", None),
            (replaced.new_bet.bet_id, 50, "active", True),
        ]
        assert bets[1].selections == sorted((seeded.entry_ids[0], seeded.entry_ids[2]))
        assert point_rows == (
            (BET_REPLACEMENT_REFUND_POINT_ACTION, 20),
            (BET_STAKE_POINT_ACTION, -50),
        )
        assert wallet_balance == 450
        assert audit.request_fingerprint == request.request_fingerprint
        assert audit.bet_id == replaced.new_bet.bet_id
        assert audit.type == "bet_replaced"
        assert audit.before_data["old_bet"]["status"] == "active"
        assert audit.after_data["old_bet"]["status"] == "cancelled"
        assert audit.after_data["new_bet"]["status"] == "active"
        assert audit.after_data["balance_after_refund"] == 500
        assert audit.after_data["balance_after"] == 450
    finally:
        _cleanup(migrated_engine, seeded)


def test_replacement_no_change_duplicate_and_stake_limit_are_zero_write(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_bet_match(migrated_engine, suffix=suffix)
    placement_commands, _, _ = _services(migrated_engine)
    replacement_commands = _replacement_commands(migrated_engine)
    old = placement_commands.place_bet(_request(seeded, key=f"old-{suffix}"))
    other = placement_commands.place_bet(
        PlaceMatchBet(
            match_id=seeded.match_id,
            bet_type=BetType.WIN,
            entry_numbers=(3,),
            amount=10,
            actor_discord_user_id=seeded.discord_user_id,
            guild_id=seeded.guild_id,
            idempotency_key=f"other-{suffix}",
            correlation_id=f"other-{suffix}",
        )
    )
    failure_keys = (
        f"no-change-{suffix}",
        f"duplicate-change-{suffix}",
        f"stake-limit-change-{suffix}",
    )
    try:
        with pytest.raises(BetReplacementNoChangeError):
            replacement_commands.replace_bet(
                _replacement_request(
                    seeded,
                    bet_id=old.bet_id,
                    key=failure_keys[0],
                    entry_numbers=(1, 2),
                    amount=20,
                )
            )
        with pytest.raises(BetReplacementDuplicateError):
            replacement_commands.replace_bet(
                _replacement_request(
                    seeded,
                    bet_id=old.bet_id,
                    key=failure_keys[1],
                    bet_type=BetType.WIN,
                    entry_numbers=(3,),
                    amount=20,
                )
            )
        with pytest.raises(BetReplacementStakeLimitError) as captured:
            replacement_commands.replace_bet(
                _replacement_request(
                    seeded,
                    bet_id=old.bet_id,
                    key=failure_keys[2],
                    amount=1000,
                )
            )
        assert captured.value.maximum_stake == 40

        with migrated_engine.connect() as connection:
            bets = tuple(
                connection.execute(
                    select(BetORM.id, BetORM.status, BetORM.active_marker)
                    .where(BetORM.match_id == seeded.match_id)
                    .order_by(BetORM.id)
                )
            )
            failed_operations = connection.scalar(
                select(func.count(OperationORM.id)).where(OperationORM.idempotency_key.in_(failure_keys))
            )
            wallet_balance = connection.scalar(
                select(CirclePointORM.balance).where(CirclePointORM.persona_id == seeded.bettor_persona_id)
            )

        assert [tuple(row) for row in bets] == [
            (old.bet_id, "active", True),
            (other.bet_id, "active", True),
        ]
        assert failed_operations == 0
        assert wallet_balance == 470
    finally:
        _cleanup(migrated_engine, seeded)


def test_concurrent_replacement_exact_retry_converges_to_one_new_bet(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_bet_match(migrated_engine, suffix=suffix)
    placement_commands, _, _ = _services(migrated_engine)
    old = placement_commands.place_bet(_request(seeded, key=f"old-concurrent-{suffix}"))
    request = _replacement_request(
        seeded,
        bet_id=old.bet_id,
        key=f"replace-concurrent-{suffix}",
    )
    start = Barrier(2)

    def run() -> int:
        commands = _replacement_commands(migrated_engine)
        start.wait()
        return commands.replace_bet(request).new_bet.bet_id

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            new_ids = tuple(future.result() for future in (executor.submit(run), executor.submit(run)))

        assert new_ids[0] == new_ids[1]
        with migrated_engine.connect() as connection:
            assert connection.scalar(select(func.count(BetORM.id)).where(BetORM.match_id == seeded.match_id)) == 2
            assert (
                connection.scalar(
                    select(func.count(BetORM.id)).where(
                        BetORM.match_id == seeded.match_id,
                        BetORM.status == "active",
                        BetORM.active_marker.is_(True),
                    )
                )
                == 1
            )
            assert (
                connection.scalar(
                    select(func.count(PointTransactionORM.id))
                    .join(OperationORM, OperationORM.id == PointTransactionORM.operation_id)
                    .where(OperationORM.idempotency_key == request.idempotency_key)
                )
                == 2
            )
            assert (
                connection.scalar(
                    select(CirclePointORM.balance).where(CirclePointORM.persona_id == seeded.bettor_persona_id)
                )
                == 470
            )
    finally:
        _cleanup(migrated_engine, seeded)


def test_concurrent_replacement_and_close_serialize_at_match_root(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_bet_match(migrated_engine, suffix=suffix)
    placement_commands, _, _ = _services(migrated_engine)
    old = placement_commands.place_bet(_request(seeded, key=f"old-race-{suffix}"))
    start = Barrier(2)

    def replace() -> bool:
        commands = _replacement_commands(migrated_engine)
        start.wait()
        try:
            commands.replace_bet(
                _replacement_request(
                    seeded,
                    bet_id=old.bet_id,
                    key=f"replace-race-{suffix}",
                )
            )
        except BetReplacementUnavailableError:
            return False
        return True

    def close() -> int:
        _, _, commands = _services(migrated_engine)
        start.wait()
        return commands.close_betting(_close_request(seeded, key=f"close-race-{suffix}")).active_bet_count

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            replaced_future = executor.submit(replace)
            close_future = executor.submit(close)
            replaced = replaced_future.result()
            close_count = close_future.result()

        assert close_count == 1
        with migrated_engine.connect() as connection:
            assert connection.scalar(select(MatchORM.status).where(MatchORM.id == seeded.match_id)) == "betting_closed"
            bet_count = connection.scalar(select(func.count(BetORM.id)).where(BetORM.match_id == seeded.match_id))
            active_count = connection.scalar(
                select(func.count(BetORM.id)).where(
                    BetORM.match_id == seeded.match_id,
                    BetORM.status == "active",
                    BetORM.active_marker.is_(True),
                )
            )
            wallet_balance = connection.scalar(
                select(CirclePointORM.balance).where(CirclePointORM.persona_id == seeded.bettor_persona_id)
            )

        assert bet_count == (2 if replaced else 1)
        assert active_count == 1
        assert wallet_balance == (470 if replaced else 480)
    finally:
        _cleanup(migrated_engine, seeded)
