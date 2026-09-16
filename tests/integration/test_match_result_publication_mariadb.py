"""MariaDB evidence for explicit native Match result publication."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Engine

from uma_st2.application.match import (
    MatchResultPublicationAuditType,
    MatchResultPublicationInvalidSourceError,
    MatchSettlementAppliedOdds,
    MatchSettlementRating,
    MatchSettlementResultAuthority,
    PublishMatchResult,
    SettledMatch,
)
from uma_st2.application.publication import (
    MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    MATCH_RESULT_CONFIRMED_EVENT_TYPE,
)
from uma_st2.compose import (
    compose_match_result_publication,
    compose_match_result_publication_queries,
    compose_publication_delivery,
)
from uma_st2.domain.betting import BetType
from uma_st2.domain.match import MatchGrade, MatchStatus
from uma_st2.domain.publication import PublicationStatus
from uma_st2.infrastructure.database import DatabaseRuntime
from uma_st2.infrastructure.database.orm import (
    BotGuildSettingORM,
    DiscordPublicationORM,
    MatchConditionORM,
    MatchOperationORM,
    MatchORM,
    OperationORM,
    StadiumCourseORM,
    StadiumORM,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class SeededPublication:
    match_id: int
    guild_id: str
    settlement_operation_id: int
    course_id: int
    stadium_id: int


def _settled(*, match_id: int, match_name: str) -> SettledMatch:
    ratings = tuple(
        MatchSettlementRating(
            match_entry_id=100 + rank,
            entry_number=rank,
            game_account_id=200 + rank,
            game_account_name=f"Publication Account {rank}",
            horse_name=f"Publication Horse {rank}",
            affiliation_at_event="A조",
            rank=rank,
            rating_before=Decimal("100.000000000000000000"),
            base_delta=Decimal("0.000000000000000000"),
            adjustment_delta=Decimal("0.000000000000000000"),
            amount=Decimal("0.000000000000000000"),
            rating_after=Decimal("100.000000000000000000"),
            rating_transaction_id=None,
        )
        for rank in range(1, 4)
    )
    odds = tuple(
        MatchSettlementAppliedOdds(
            bet_type=bet_type,
            selection_entry_ids=tuple(100 + rank for rank in range(1, selection_count + 1)),
            selection_entry_numbers=tuple(range(1, selection_count + 1)),
            provisional_odds=Decimal(f"{selection_count}.0000"),
            confirmed_odds=Decimal(f"{selection_count}.0"),
        )
        for selection_count, bet_type in enumerate(BetType, start=1)
    )
    return SettledMatch(
        match_id=match_id,
        match_name=match_name,
        previous_status=MatchStatus.RESULT_CONFIRMED,
        status=MatchStatus.SETTLED,
        grade=MatchGrade.OP,
        settled_at=NOW,
        result=MatchSettlementResultAuthority(301, 1, "a" * 64),
        settlement_fingerprint="b" * 64,
        active_bet_ids=(),
        active_stake_total=0,
        applied_odds=odds,
        payouts=(),
        rewards=(),
        rating_rule_version=None,
        ratings=ratings,
    )


def _seed(engine: Engine, *, suffix: str) -> SeededPublication:
    stored_now = (NOW - timedelta(days=1)).replace(tzinfo=None)
    external_base = int(suffix[:12], 16)
    guild_id = str(int(suffix[:15], 16) + 1)
    match_name = f"Publication Match {suffix}"
    with engine.begin() as connection:
        stadium_id = connection.execute(
            StadiumORM.__table__.insert().values(
                external_id=external_base,
                name_jp=f"Tokyo {suffix}",
                name_ko=f"도쿄 {suffix}",
                created_at=stored_now,
                updated_at=stored_now,
            )
        ).inserted_primary_key[0]
        course_id = connection.execute(
            StadiumCourseORM.__table__.insert().values(
                stadium_id=stadium_id,
                external_id=external_base + 1,
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
                name=match_name,
                description="공식 룸매치",
                source_kind="native_v2",
                grade="OP",
                stadium_course_id=course_id,
                scheduled_at=SCHEDULED_AT.replace(tzinfo=None),
                status="settled",
                terminal_reason=None,
                finish_time_ms=123400,
                created_at=stored_now,
                updated_at=stored_now,
            )
        ).inserted_primary_key[0]
        connection.execute(
            MatchConditionORM.__table__.insert().values(
                match_id=match_id,
                season="autumn",
                weather="sunny",
                time_of_day="night",
                track_condition="firm",
                created_at=stored_now,
                updated_at=stored_now,
            )
        )
        connection.execute(
            BotGuildSettingORM.__table__.insert().values(
                guild_id=guild_id,
                win5_announcement_channel_id=None,
                match_announcement_channel_id="777777777",
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
        settlement_operation_id = connection.execute(
            OperationORM.__table__.insert().values(
                guild_id=guild_id,
                correlation_id=f"settlement-{suffix}",
                actor_discord_user_id="123456789",
                idempotency_key=f"settlement-{suffix}",
                request_fingerprint="c" * 64,
                reason=None,
                created_at=stored_now,
            )
        ).inserted_primary_key[0]
        connection.execute(
            MatchOperationORM.__table__.insert().values(
                operation_id=settlement_operation_id,
                match_id=match_id,
                type="match_settled",
                before_data={"schema_version": 1},
                after_data=_settled(match_id=match_id, match_name=match_name).to_audit_payload(),
            )
        )
    return SeededPublication(match_id, guild_id, settlement_operation_id, course_id, stadium_id)


def _request(seeded: SeededPublication, *, key: str) -> PublishMatchResult:
    return PublishMatchResult(
        match_id=seeded.match_id,
        idempotency_key=key,
        actor_discord_user_id="123456789",
        guild_id=seeded.guild_id,
        correlation_id=key,
    )


def _cleanup(engine: Engine, seeded: SeededPublication) -> None:
    with engine.begin() as connection:
        operation_ids = tuple(
            connection.scalars(
                select(MatchOperationORM.operation_id).where(MatchOperationORM.match_id == seeded.match_id)
            )
        )
        connection.execute(delete(DiscordPublicationORM).where(DiscordPublicationORM.source_id == seeded.match_id))
        connection.execute(delete(MatchOperationORM).where(MatchOperationORM.match_id == seeded.match_id))
        if operation_ids:
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(delete(BotGuildSettingORM).where(BotGuildSettingORM.guild_id == seeded.guild_id))
        connection.execute(delete(MatchConditionORM).where(MatchConditionORM.match_id == seeded.match_id))
        connection.execute(delete(MatchORM).where(MatchORM.id == seeded.match_id))
        connection.execute(delete(StadiumCourseORM).where(StadiumCourseORM.id == seeded.course_id))
        connection.execute(delete(StadiumORM).where(StadiumORM.id == seeded.stadium_id))


def test_publish_commits_one_intent_audit_exact_retry_and_delivery_route(migrated_engine: Engine) -> None:
    seeded = _seed(migrated_engine, suffix=uuid4().hex)
    runtime = DatabaseRuntime.from_engine(migrated_engine)
    commands = compose_match_result_publication(runtime)
    queries = compose_match_result_publication_queries(runtime)
    delivery = compose_publication_delivery(runtime)
    request = _request(seeded, key=f"match-result-publish-{uuid4().hex}")

    try:
        assert {target.match_id for target in queries.search_targets(search=str(seeded.match_id))} == {seeded.match_id}
        result = commands.publish_result(request)
        assert result == commands.publish_result(request)
        assert result.publication.status is PublicationStatus.READY
        assert queries.search_targets(search=str(seeded.match_id)) == ()

        with migrated_engine.connect() as connection:
            publication = connection.execute(
                select(
                    DiscordPublicationORM.destination_kind,
                    DiscordPublicationORM.event_type,
                    DiscordPublicationORM.target_channel_id,
                    DiscordPublicationORM.payload_json,
                ).where(DiscordPublicationORM.id == result.publication.publication_id)
            ).one()
            publish_audit = connection.execute(
                select(MatchOperationORM.after_data).where(
                    MatchOperationORM.match_id == seeded.match_id,
                    MatchOperationORM.type == MatchResultPublicationAuditType.PUBLISHED.value,
                )
            ).scalar_one()
        assert publication.destination_kind == MATCH_ANNOUNCEMENT_DESTINATION_KIND
        assert publication.event_type == MATCH_RESULT_CONFIRMED_EVENT_TYPE
        assert publication.target_channel_id == "777777777"
        assert publication.payload_json["results"][0]["player_name"] == "Publication Account 1"
        assert publication.payload_json["odds"]["markets"][2]["confirmed_odds"] == "3.0"
        assert publish_audit["publication"]["publication_id"] == result.publication.publication_id

        claimed = delivery.claim_next(retry_delay=timedelta(minutes=5), max_attempts=3)
        assert claimed is not None
        assert claimed.publication_id == result.publication.publication_id
        assert claimed.destination_kind == MATCH_ANNOUNCEMENT_DESTINATION_KIND
        assert claimed.event_type == MATCH_RESULT_CONFIRMED_EVENT_TYPE
    finally:
        _cleanup(migrated_engine, seeded)


def test_concurrent_exact_retry_converges_to_one_publication_bundle(migrated_engine: Engine) -> None:
    seeded = _seed(migrated_engine, suffix=uuid4().hex)
    commands = compose_match_result_publication(DatabaseRuntime.from_engine(migrated_engine))
    request = _request(seeded, key=f"match-result-publish-{uuid4().hex}")
    barrier = Barrier(2)

    def publish_after_barrier() -> object:
        barrier.wait()
        return commands.publish_result(request)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(lambda _: publish_after_barrier(), range(2)))
        assert results[0] == results[1]
        with migrated_engine.connect() as connection:
            publication_count = connection.scalar(
                select(func.count(DiscordPublicationORM.id)).where(
                    DiscordPublicationORM.source_id == seeded.match_id,
                    DiscordPublicationORM.event_type == MATCH_RESULT_CONFIRMED_EVENT_TYPE,
                )
            )
            audit_count = connection.scalar(
                select(func.count(MatchOperationORM.operation_id)).where(
                    MatchOperationORM.match_id == seeded.match_id,
                    MatchOperationORM.type == MatchResultPublicationAuditType.PUBLISHED.value,
                )
            )
        assert publication_count == 1
        assert audit_count == 1
    finally:
        _cleanup(migrated_engine, seeded)


def test_malformed_settlement_evidence_is_zero_write(migrated_engine: Engine) -> None:
    seeded = _seed(migrated_engine, suffix=uuid4().hex)
    commands = compose_match_result_publication(DatabaseRuntime.from_engine(migrated_engine))
    with migrated_engine.begin() as connection:
        connection.execute(
            update(MatchOperationORM)
            .where(MatchOperationORM.operation_id == seeded.settlement_operation_id)
            .values(after_data={"schema_version": 1, "malformed": True})
        )

    try:
        with pytest.raises(MatchResultPublicationInvalidSourceError):
            commands.publish_result(_request(seeded, key=f"match-result-publish-{uuid4().hex}"))
        with migrated_engine.connect() as connection:
            publication_count = connection.scalar(
                select(func.count(DiscordPublicationORM.id)).where(DiscordPublicationORM.source_id == seeded.match_id)
            )
            audit_count = connection.scalar(
                select(func.count(MatchOperationORM.operation_id)).where(
                    MatchOperationORM.match_id == seeded.match_id,
                    MatchOperationORM.type == MatchResultPublicationAuditType.PUBLISHED.value,
                )
            )
        assert publication_count == 0
        assert audit_count == 0
    finally:
        _cleanup(migrated_engine, seeded)
