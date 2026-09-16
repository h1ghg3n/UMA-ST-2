"""MariaDB atomic Match close, aggregate, audit, and exact-retry evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.match import (
    CloseMatchBetting,
    MatchBettingCloseCommands,
    MatchBettingCloseInvalidSourceError,
    MatchBettingCloseUnavailableError,
    MatchStaffBettingCloseQueries,
)
from uma_st2.application.publication import MATCH_BETTING_CLOSED_EVENT_TYPE
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyMatchBettingCloseUnitOfWorkFactory,
    SqlAlchemyMatchStaffBettingCloseQueryUnitOfWorkFactory,
)
from uma_st2.infrastructure.database.orm import (
    BetORM,
    DiscordPublicationORM,
    GameAccountORM,
    MatchEntryORM,
    MatchOperationORM,
    MatchORM,
    OperationORM,
    PersonaORM,
    StadiumCourseORM,
    StadiumORM,
    UmamusumeORM,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class SeededCloseMatch:
    match_id: int
    guild_id: str
    stadium_id: int
    course_id: int
    persona_ids: tuple[str, ...]
    account_ids: tuple[int, ...]
    umamusume_ids: tuple[int, ...]
    entry_ids: tuple[int, ...]
    bet_ids: tuple[int, ...]


def _seed_close_match(
    engine: Engine,
    *,
    suffix: str,
    with_bets: bool = True,
    inconsistent_marker: bool = False,
) -> SeededCloseMatch:
    stored_now = (NOW - timedelta(days=1)).replace(tzinfo=None)
    external_base = int(suffix[:12], 16)
    guild_id = str(int(suffix[:15], 16) + 1)
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
                name=f"Close Match {suffix}",
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
        bet_ids: tuple[int, ...] = ()
        if with_bets:
            first = connection.execute(
                BetORM.__table__.insert().values(
                    match_id=match_id,
                    persona_id=persona_ids[0],
                    type="win",
                    selections=[entry_ids[0]],
                    selection_fingerprint="a" * 64,
                    amount=10,
                    status="active",
                    active_marker=None if inconsistent_marker else True,
                    created_at=stored_now,
                    updated_at=stored_now,
                )
            ).inserted_primary_key[0]
            second = connection.execute(
                BetORM.__table__.insert().values(
                    match_id=match_id,
                    persona_id=persona_ids[1],
                    type="quinella",
                    selections=[entry_ids[0], entry_ids[1]],
                    selection_fingerprint="b" * 64,
                    amount=30,
                    status="active",
                    active_marker=True,
                    created_at=stored_now,
                    updated_at=stored_now,
                )
            ).inserted_primary_key[0]
            bet_ids = (first, second)
    return SeededCloseMatch(
        match_id=match_id,
        guild_id=guild_id,
        stadium_id=stadium_id,
        course_id=course_id,
        persona_ids=persona_ids,
        account_ids=account_ids,
        umamusume_ids=umamusume_ids,
        entry_ids=entry_ids,
        bet_ids=bet_ids,
    )


def _services(engine: Engine) -> tuple[MatchBettingCloseCommands, MatchStaffBettingCloseQueries]:
    runtime = DatabaseRuntime.from_engine(engine)
    return (
        MatchBettingCloseCommands(
            CommandRunner(SqlAlchemyMatchBettingCloseUnitOfWorkFactory(runtime.session_factory)),
            clock=lambda: NOW,
        ),
        MatchStaffBettingCloseQueries(
            QueryRunner(SqlAlchemyMatchStaffBettingCloseQueryUnitOfWorkFactory(runtime.session_factory))
        ),
    )


def _request(seeded: SeededCloseMatch, *, key: str) -> CloseMatchBetting:
    return CloseMatchBetting(
        match_id=seeded.match_id,
        idempotency_key=key,
        actor_discord_user_id="123456789",
        guild_id=seeded.guild_id,
        correlation_id=key,
    )


def _cleanup(engine: Engine, seeded: SeededCloseMatch) -> None:
    with engine.begin() as connection:
        operation_ids = tuple(
            connection.scalars(
                select(MatchOperationORM.operation_id).where(MatchOperationORM.match_id == seeded.match_id)
            )
        )
        connection.execute(delete(BetORM).where(BetORM.match_id == seeded.match_id))
        connection.execute(
            delete(DiscordPublicationORM).where(
                DiscordPublicationORM.source_kind == "match",
                DiscordPublicationORM.source_id == seeded.match_id,
            )
        )
        if operation_ids:
            connection.execute(delete(MatchOperationORM).where(MatchOperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(delete(MatchEntryORM).where(MatchEntryORM.match_id == seeded.match_id))
        connection.execute(delete(MatchORM).where(MatchORM.id == seeded.match_id))
        connection.execute(delete(StadiumCourseORM).where(StadiumCourseORM.id == seeded.course_id))
        connection.execute(delete(StadiumORM).where(StadiumORM.id == seeded.stadium_id))
        connection.execute(delete(UmamusumeORM).where(UmamusumeORM.id.in_(seeded.umamusume_ids)))
        connection.execute(delete(GameAccountORM).where(GameAccountORM.id.in_(seeded.account_ids)))
        connection.execute(delete(PersonaORM).where(PersonaORM.id.in_(seeded.persona_ids)))


def test_close_transition_preserves_bets_and_records_final_aggregate_exactly_once(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_close_match(migrated_engine, suffix=suffix)
    commands, queries = _services(migrated_engine)
    request = _request(seeded, key=f"close-{suffix}")
    try:
        choices = queries.search_targets(search=suffix, limit=25)
        assert [(choice.match_id, choice.active_bet_count) for choice in choices] == [(seeded.match_id, 2)]
        target = queries.get_target(match_id=seeded.match_id)
        assert (target.entry_count, target.active_bet_count, target.active_stake_total) == (3, 2, 40)

        closed = commands.close_betting(request)
        assert commands.close_betting(request) == closed

        with migrated_engine.connect() as connection:
            stored_status = connection.scalar(select(MatchORM.status).where(MatchORM.id == seeded.match_id))
            bet_rows = tuple(
                connection.execute(
                    select(BetORM.id, BetORM.status, BetORM.amount)
                    .where(BetORM.match_id == seeded.match_id)
                    .order_by(BetORM.id)
                )
            )
            audit = connection.execute(
                select(MatchOperationORM.before_data, MatchOperationORM.after_data)
                .join(OperationORM, OperationORM.id == MatchOperationORM.operation_id)
                .where(
                    MatchOperationORM.match_id == seeded.match_id,
                    MatchOperationORM.type == "match_betting_closed",
                )
            ).one()
            publication = connection.execute(
                select(
                    DiscordPublicationORM.id,
                    DiscordPublicationORM.event_type,
                    DiscordPublicationORM.event_key,
                    DiscordPublicationORM.status,
                    DiscordPublicationORM.payload_json,
                ).where(
                    DiscordPublicationORM.source_kind == "match",
                    DiscordPublicationORM.source_id == seeded.match_id,
                    DiscordPublicationORM.event_type == MATCH_BETTING_CLOSED_EVENT_TYPE,
                )
            ).one()

        assert stored_status == "betting_closed"
        assert [(row.id, row.status, row.amount) for row in bet_rows] == [
            (seeded.bet_ids[0], "active", 10),
            (seeded.bet_ids[1], "active", 30),
        ]
        assert audit.before_data["status"] == "betting_open"
        assert audit.before_data["schema_version"] == 2
        assert audit.after_data["status"] == "betting_closed"
        assert audit.after_data["schema_version"] == 2
        assert audit.after_data["active_bet_count"] == 2
        assert audit.after_data["active_stake_total"] == 40
        assert audit.after_data["publication"]["publication_id"] == publication.id
        assert publication.event_type == MATCH_BETTING_CLOSED_EVENT_TYPE
        assert publication.event_key == f"match:{seeded.match_id}:betting-close:v1"
        assert publication.status == "awaiting_channel"
        assert publication.payload_json["publication_type"] == "match_betting_closed"
        assert publication.payload_json["odds"]["field_multiplier"] == "0.5"
        assert [market["bet_type"] for market in publication.payload_json["odds"]["markets"]] == [
            "win",
            "quinella",
            "trio",
        ]

        with pytest.raises(MatchBettingCloseUnavailableError):
            commands.close_betting(_request(seeded, key=f"repeat-{suffix}"))
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count(MatchOperationORM.operation_id)).where(
                        MatchOperationORM.match_id == seeded.match_id,
                        MatchOperationORM.type == "match_betting_closed",
                    )
                )
                == 1
            )
    finally:
        _cleanup(migrated_engine, seeded)


def test_zero_bet_close_and_inconsistent_active_marker_boundaries(migrated_engine: Engine) -> None:
    zero_suffix = uuid4().hex
    zero_seeded = _seed_close_match(migrated_engine, suffix=zero_suffix, with_bets=False)
    commands, queries = _services(migrated_engine)
    try:
        target = queries.get_target(match_id=zero_seeded.match_id)
        assert (target.active_bet_count, target.active_stake_total) == (0, 0)
        closed = commands.close_betting(_request(zero_seeded, key=f"zero-{zero_suffix}"))
        assert (closed.active_bet_count, closed.active_stake_total) == (0, 0)
        with migrated_engine.connect() as connection:
            payload = connection.scalar(
                select(DiscordPublicationORM.payload_json).where(
                    DiscordPublicationORM.source_kind == "match",
                    DiscordPublicationORM.source_id == zero_seeded.match_id,
                    DiscordPublicationORM.event_type == MATCH_BETTING_CLOSED_EVENT_TYPE,
                )
            )
        assert payload is not None
        assert sum(len(market["selections"]) for market in payload["odds"]["markets"]) == 7
    finally:
        _cleanup(migrated_engine, zero_seeded)

    invalid_suffix = uuid4().hex
    invalid_seeded = _seed_close_match(
        migrated_engine,
        suffix=invalid_suffix,
        inconsistent_marker=True,
    )
    commands, _ = _services(migrated_engine)
    try:
        with pytest.raises(MatchBettingCloseInvalidSourceError):
            commands.close_betting(_request(invalid_seeded, key=f"invalid-{invalid_suffix}"))
        with migrated_engine.connect() as connection:
            assert connection.scalar(select(MatchORM.status).where(MatchORM.id == invalid_seeded.match_id)) == (
                "betting_open"
            )
            assert (
                connection.scalar(
                    select(func.count(MatchOperationORM.operation_id)).where(
                        MatchOperationORM.match_id == invalid_seeded.match_id
                    )
                )
                == 0
            )
            assert (
                connection.scalar(
                    select(func.count(DiscordPublicationORM.id)).where(
                        DiscordPublicationORM.source_kind == "match",
                        DiscordPublicationORM.source_id == invalid_seeded.match_id,
                    )
                )
                == 0
            )
    finally:
        _cleanup(migrated_engine, invalid_seeded)


def test_close_publication_constraint_failure_rolls_back_match_and_audit(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_close_match(migrated_engine, suffix=suffix)
    stored_now = NOW.replace(tzinfo=None)
    with migrated_engine.begin() as connection:
        connection.execute(
            DiscordPublicationORM.__table__.insert().values(
                guild_id=seeded.guild_id,
                destination_kind="match_announcement",
                event_type=MATCH_BETTING_CLOSED_EVENT_TYPE,
                event_key=f"match:{seeded.match_id}:betting-close:v1",
                source_kind="match",
                source_id=seeded.match_id,
                target_channel_id=None,
                payload_json={"preexisting": True},
                payload_fingerprint="f" * 64,
                status="suppressed",
                attempt_count=0,
                discord_message_id=None,
                last_error_code=None,
                failure_stage=None,
                attempt_started_at=None,
                published_at=None,
                created_at=stored_now,
                updated_at=stored_now,
            )
        )
    commands, _ = _services(migrated_engine)
    try:
        with pytest.raises(IntegrityError):
            commands.close_betting(_request(seeded, key=f"constraint-{suffix}"))

        with migrated_engine.connect() as connection:
            assert connection.scalar(select(MatchORM.status).where(MatchORM.id == seeded.match_id)) == "betting_open"
            assert (
                connection.scalar(
                    select(func.count(MatchOperationORM.operation_id)).where(
                        MatchOperationORM.match_id == seeded.match_id,
                        MatchOperationORM.type == "match_betting_closed",
                    )
                )
                == 0
            )
            assert (
                connection.scalar(
                    select(func.count(DiscordPublicationORM.id)).where(
                        DiscordPublicationORM.source_kind == "match",
                        DiscordPublicationORM.source_id == seeded.match_id,
                    )
                )
                == 1
            )
    finally:
        _cleanup(migrated_engine, seeded)
