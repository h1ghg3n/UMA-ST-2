"""MariaDB atomic whole-Match cancellation and refund evidence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.match import (
    MATCH_BET_REFUND_POINT_ACTION,
    CancelMatch,
    MatchCancellationCommands,
    MatchCancellationUnavailableError,
    MatchCancellationWalletUnavailableError,
    MatchStaffCancellationQueries,
)
from uma_st2.application.publication import (
    MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    MATCH_BETS_REFUNDED_EVENT_TYPE,
)
from uma_st2.compose import compose_publication_delivery
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyMatchCancellationUnitOfWorkFactory,
    SqlAlchemyMatchStaffCancellationQueryUnitOfWorkFactory,
)
from uma_st2.infrastructure.database.orm import (
    BetORM,
    BotGuildSettingORM,
    CirclePointORM,
    DiscordPublicationORM,
    MatchOperationORM,
    MatchORM,
    OperationORM,
    PersonaORM,
    PointTransactionORM,
    StadiumCourseORM,
    StadiumORM,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 28, 11, 0, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class SeededCancellation:
    match_id: int
    guild_id: str
    channel_id: str
    persona_ids: tuple[str, ...]
    bet_ids: tuple[int, ...]
    stadium_id: int
    course_id: int


def _seed_cancellation(engine: Engine, *, suffix: str, with_bets: bool = True) -> SeededCancellation:
    stored_now = (NOW - timedelta(days=1)).replace(tzinfo=None)
    external_base = int(suffix[:12], 16)
    guild_id = str(int(suffix[:15], 16) + 1)
    channel_id = str(int(suffix[1:16], 16) + 1)
    persona_ids = (str(uuid4()), str(uuid4()))
    with engine.begin() as connection:
        connection.execute(
            PersonaORM.__table__.insert(),
            [
                {
                    "id": persona_id,
                    "display_name": f"Cancellation Persona {index} {suffix}",
                    "status": "normal",
                    "created_at": stored_now,
                    "updated_at": stored_now,
                }
                for index, persona_id in enumerate(persona_ids, start=1)
            ],
        )
        connection.execute(
            CirclePointORM.__table__.insert(),
            [
                {"persona_id": persona_ids[0], "balance": 100, "updated_at": stored_now},
                {"persona_id": persona_ids[1], "balance": 200, "updated_at": stored_now},
            ],
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
                name=f"Cancellation Match {suffix}",
                description="official no-contest",
                source_kind="native_v2",
                grade="G1",
                stadium_course_id=course_id,
                scheduled_at=SCHEDULED_AT.replace(tzinfo=None),
                status="betting_closed",
                terminal_reason=None,
                finish_time_ms=None,
                created_at=stored_now,
                updated_at=stored_now,
            )
        ).inserted_primary_key[0]
        bet_specs = (
            (
                (persona_ids[0], "win", [101], 10),
                (persona_ids[0], "quinella", [101, 102], 20),
                (persona_ids[1], "trio", [101, 102, 103], 30),
            )
            if with_bets
            else ()
        )
        bet_ids = tuple(
            connection.execute(
                BetORM.__table__.insert().values(
                    match_id=match_id,
                    persona_id=persona_id,
                    type=bet_type,
                    selections=selections,
                    selection_fingerprint=f"{suffix}-{index}",
                    amount=amount,
                    status="active",
                    active_marker=True,
                    created_at=stored_now,
                    updated_at=stored_now,
                )
            ).inserted_primary_key[0]
            for index, (persona_id, bet_type, selections, amount) in enumerate(bet_specs, start=1)
        )
        connection.execute(
            BotGuildSettingORM.__table__.insert().values(
                guild_id=guild_id,
                win5_announcement_channel_id=None,
                match_announcement_channel_id=channel_id,
                log_channel_id=None,
                operator_role_id=None,
                bot_manager_role_id=None,
                default_timezone="Asia/Seoul",
                win5_announcements_enabled=True,
                match_announcements_enabled=True,
                created_at=stored_now,
                updated_at=stored_now,
            )
        )
    return SeededCancellation(
        match_id=match_id,
        guild_id=guild_id,
        channel_id=channel_id,
        persona_ids=persona_ids,
        bet_ids=bet_ids,
        stadium_id=stadium_id,
        course_id=course_id,
    )


def _services(engine: Engine) -> tuple[MatchCancellationCommands, MatchStaffCancellationQueries]:
    runtime = DatabaseRuntime.from_engine(engine)
    return (
        MatchCancellationCommands(
            CommandRunner(SqlAlchemyMatchCancellationUnitOfWorkFactory(runtime.session_factory)),
            clock=lambda: NOW,
        ),
        MatchStaffCancellationQueries(
            QueryRunner(SqlAlchemyMatchStaffCancellationQueryUnitOfWorkFactory(runtime.session_factory))
        ),
    )


def _request(seeded: SeededCancellation, *, key: str, reason: str = "공식 경기 취소") -> CancelMatch:
    return CancelMatch(
        match_id=seeded.match_id,
        reason=reason,
        idempotency_key=key,
        actor_discord_user_id="operator-1",
        guild_id=seeded.guild_id,
        correlation_id=key,
    )


def _cleanup(engine: Engine, seeded: SeededCancellation) -> None:
    with engine.begin() as connection:
        operation_ids = tuple(
            connection.scalars(
                select(MatchOperationORM.operation_id).where(MatchOperationORM.match_id == seeded.match_id)
            )
        )
        if operation_ids:
            connection.execute(delete(PointTransactionORM).where(PointTransactionORM.operation_id.in_(operation_ids)))
            connection.execute(delete(MatchOperationORM).where(MatchOperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(delete(DiscordPublicationORM).where(DiscordPublicationORM.source_id == seeded.match_id))
        connection.execute(delete(BetORM).where(BetORM.match_id == seeded.match_id))
        connection.execute(delete(CirclePointORM).where(CirclePointORM.persona_id.in_(seeded.persona_ids)))
        connection.execute(delete(MatchORM).where(MatchORM.id == seeded.match_id))
        connection.execute(delete(StadiumCourseORM).where(StadiumCourseORM.id == seeded.course_id))
        connection.execute(delete(StadiumORM).where(StadiumORM.id == seeded.stadium_id))
        connection.execute(delete(PersonaORM).where(PersonaORM.id.in_(seeded.persona_ids)))
        connection.execute(delete(BotGuildSettingORM).where(BotGuildSettingORM.guild_id == seeded.guild_id))


def test_cancellation_refunds_active_bets_and_preserves_exact_audit(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_cancellation(migrated_engine, suffix=suffix)
    commands, queries = _services(migrated_engine)
    request = _request(seeded, key=f"match-cancel-{suffix}")
    try:
        target = queries.get_target(match_id=seeded.match_id)
        assert (target.active_bet_count, target.active_stake_total, target.affected_persona_count) == (3, 60, 2)

        cancelled = commands.cancel_match(request)
        assert commands.cancel_match(request) == cancelled
        with pytest.raises(MatchCancellationUnavailableError):
            commands.cancel_match(_request(seeded, key=f"changed-key-{suffix}"))

        with migrated_engine.connect() as connection:
            match = connection.execute(
                select(MatchORM.status, MatchORM.terminal_reason).where(MatchORM.id == seeded.match_id)
            ).one()
            bets = tuple(
                connection.execute(
                    select(BetORM.id, BetORM.status, BetORM.active_marker)
                    .where(BetORM.match_id == seeded.match_id)
                    .order_by(BetORM.id)
                )
            )
            balances = tuple(
                connection.execute(
                    select(CirclePointORM.persona_id, CirclePointORM.balance)
                    .where(CirclePointORM.persona_id.in_(seeded.persona_ids))
                    .order_by(CirclePointORM.persona_id)
                )
            )
            point_rows = tuple(
                connection.execute(
                    select(
                        PointTransactionORM.id,
                        PointTransactionORM.persona_id,
                        PointTransactionORM.action,
                        PointTransactionORM.amount,
                    )
                    .join(OperationORM, OperationORM.id == PointTransactionORM.operation_id)
                    .where(OperationORM.idempotency_key == request.idempotency_key)
                    .order_by(PointTransactionORM.persona_id)
                )
            )
            audit = connection.execute(
                select(
                    OperationORM.reason,
                    MatchOperationORM.type,
                    MatchOperationORM.before_data,
                    MatchOperationORM.after_data,
                )
                .join(MatchOperationORM, MatchOperationORM.operation_id == OperationORM.id)
                .where(OperationORM.idempotency_key == request.idempotency_key)
            ).one()
            publication = connection.execute(
                select(
                    DiscordPublicationORM.id,
                    DiscordPublicationORM.status,
                    DiscordPublicationORM.target_channel_id,
                    DiscordPublicationORM.event_type,
                    DiscordPublicationORM.event_key,
                    DiscordPublicationORM.payload_json,
                    DiscordPublicationORM.payload_fingerprint,
                ).where(
                    DiscordPublicationORM.source_id == seeded.match_id,
                    DiscordPublicationORM.event_type == MATCH_BETS_REFUNDED_EVENT_TYPE,
                )
            ).one()

        assert match == ("cancelled", "공식 경기 취소")
        assert bets == tuple((bet_id, "cancelled", None) for bet_id in seeded.bet_ids)
        expected_balances = tuple(sorted(((seeded.persona_ids[0], 130), (seeded.persona_ids[1], 230))))
        assert balances == expected_balances
        assert tuple((row.action, row.amount) for row in point_rows) == (
            (MATCH_BET_REFUND_POINT_ACTION, 30),
            (MATCH_BET_REFUND_POINT_ACTION, 30),
        )
        assert audit.reason == "공식 경기 취소"
        assert audit.type == "match_cancelled"
        assert audit.before_data["status"] == "betting_closed"
        assert audit.after_data["status"] == "cancelled"
        assert audit.after_data["cancelled_bet_count"] == 3
        assert audit.after_data["refund_total"] == 60
        assert audit.after_data["schema_version"] == 2
        assert sorted(refund["point_transaction_id"] for refund in audit.after_data["refunds"]) == sorted(
            row.id for row in point_rows
        )
        assert cancelled.refund_total == 60
        assert cancelled.publication is not None
        assert publication.id == cancelled.publication.publication_id
        assert publication.status == "ready"
        assert publication.target_channel_id == seeded.channel_id
        assert publication.event_key == f"match:{seeded.match_id}:bet-refund:v1"
        assert publication.payload_json["refund"] == {
            "completed_at": NOW.isoformat(),
            "full_original_stake": True,
            "reason": "공식 경기 취소",
        }
        assert set(publication.payload_json) == {"schema_version", "publication_type", "match", "refund"}
        assert publication.payload_fingerprint == cancelled.publication.payload_fingerprint

        claimed = compose_publication_delivery(DatabaseRuntime.from_engine(migrated_engine)).claim_next(
            retry_delay=timedelta(minutes=5),
            max_attempts=3,
        )
        assert claimed is not None
        assert claimed.publication_id == publication.id
        assert claimed.destination_kind == MATCH_ANNOUNCEMENT_DESTINATION_KIND
        assert claimed.event_type == MATCH_BETS_REFUNDED_EVENT_TYPE
    finally:
        _cleanup(migrated_engine, seeded)


def test_concurrent_exact_retry_creates_one_refund_bundle(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_cancellation(migrated_engine, suffix=suffix)
    request = _request(seeded, key=f"concurrent-match-cancel-{suffix}")
    start = Barrier(2)

    def run() -> int:
        commands, _ = _services(migrated_engine)
        start.wait()
        return commands.cancel_match(request).refund_total

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            totals = tuple(future.result() for future in (executor.submit(run), executor.submit(run)))

        assert totals == (60, 60)
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count(MatchOperationORM.operation_id)).where(
                        MatchOperationORM.match_id == seeded.match_id,
                        MatchOperationORM.type == "match_cancelled",
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
                    select(func.count(DiscordPublicationORM.id)).where(
                        DiscordPublicationORM.source_id == seeded.match_id,
                        DiscordPublicationORM.event_type == MATCH_BETS_REFUNDED_EVENT_TYPE,
                    )
                )
                == 1
            )
    finally:
        _cleanup(migrated_engine, seeded)


def test_missing_wallet_leaves_match_bets_and_other_wallet_unchanged(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_cancellation(migrated_engine, suffix=suffix)
    request = _request(seeded, key=f"missing-wallet-match-cancel-{suffix}")
    with migrated_engine.begin() as connection:
        connection.execute(delete(CirclePointORM).where(CirclePointORM.persona_id == seeded.persona_ids[1]))
    commands, _ = _services(migrated_engine)
    try:
        with pytest.raises(MatchCancellationWalletUnavailableError):
            commands.cancel_match(request)

        with migrated_engine.connect() as connection:
            assert connection.scalar(select(MatchORM.status).where(MatchORM.id == seeded.match_id)) == "betting_closed"
            assert (
                connection.scalar(
                    select(func.count(BetORM.id)).where(
                        BetORM.match_id == seeded.match_id,
                        BetORM.status == "active",
                        BetORM.active_marker.is_(True),
                    )
                )
                == 3
            )
            assert (
                connection.scalar(
                    select(CirclePointORM.balance).where(CirclePointORM.persona_id == seeded.persona_ids[0])
                )
                == 100
            )
            assert (
                connection.scalar(
                    select(func.count(OperationORM.id)).where(OperationORM.idempotency_key == request.idempotency_key)
                )
                == 0
            )
            assert (
                connection.scalar(
                    select(func.count(DiscordPublicationORM.id)).where(
                        DiscordPublicationORM.source_id == seeded.match_id
                    )
                )
                == 0
            )
    finally:
        _cleanup(migrated_engine, seeded)


def test_zero_bet_cancellation_allows_omitted_reason_without_publication(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_cancellation(migrated_engine, suffix=suffix, with_bets=False)
    commands, _ = _services(migrated_engine)
    request = CancelMatch(
        match_id=seeded.match_id,
        reason=None,
        idempotency_key=f"zero-bet-match-cancel-{suffix}",
        actor_discord_user_id="operator-1",
        guild_id=seeded.guild_id,
        correlation_id=f"zero-bet-match-cancel-{suffix}",
    )
    try:
        cancelled = commands.cancel_match(request)

        assert cancelled.reason is None
        assert cancelled.refunds == ()
        assert cancelled.publication is None
        with migrated_engine.connect() as connection:
            match = connection.execute(
                select(MatchORM.status, MatchORM.terminal_reason).where(MatchORM.id == seeded.match_id)
            ).one()
            publication_count = connection.scalar(
                select(func.count(DiscordPublicationORM.id)).where(DiscordPublicationORM.source_id == seeded.match_id)
            )
        assert match == ("cancelled", None)
        assert publication_count == 0
    finally:
        _cleanup(migrated_engine, seeded)
