"""MariaDB transaction evidence for Special WIN5 scoring."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine

from uma_st2.application.win5 import ScoreSpecialWin5Round
from uma_st2.compose import compose_win5_special_scoring
from uma_st2.domain.win5 import (
    WIN5_SPECIAL_SCORING_POLICY_VERSION,
    WIN5_SPECIAL_VOID_SCORING_POLICY_VERSION,
)
from uma_st2.infrastructure.database import DatabaseRuntime
from uma_st2.infrastructure.database.orm import (
    DiscordPublicationORM,
    OperationORM,
    PersonaORM,
    PointTransactionORM,
    Win5OperationORM,
    Win5RaceORM,
    Win5ResultORM,
    Win5RoundORM,
    Win5ScoreEventItemORM,
    Win5ScoreEventORM,
    Win5ScoreORM,
    Win5SeasonORM,
    Win5SubmissionORM,
    Win5SubmissionPickORM,
)

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True)
class SeededSpecialScoringRound:
    season_id: int
    round_id: int
    race_ids: tuple[int, ...]
    result_ids: tuple[int, ...]
    persona_ids: tuple[str, ...]
    submission_ids: tuple[int, ...]


def _seed_special_scoring_round(
    engine: Engine,
    *,
    suffix: str,
    void_positions: tuple[int, ...] = (),
) -> SeededSpecialScoringRound:
    now = datetime.now(UTC).replace(tzinfo=None)
    void_position_set = set(void_positions)
    persona_ids = (str(uuid4()), str(uuid4()))
    with engine.begin() as connection:
        for index, persona_id in enumerate(persona_ids, start=1):
            connection.execute(
                PersonaORM.__table__.insert().values(
                    id=persona_id,
                    display_name=f"Special scoring persona {index}",
                    status="normal",
                    created_at=now,
                    updated_at=now,
                )
            )
        season_id = connection.execute(
            Win5SeasonORM.__table__.insert().values(
                name=f"Special scoring Season {suffix}",
                status="active",
                active_marker=True,
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        round_id = connection.execute(
            Win5RoundORM.__table__.insert().values(
                season_id=season_id,
                type="special",
                status="closed",
                name=f"Special scoring Round {suffix}",
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        race_ids = tuple(
            connection.execute(
                Win5RaceORM.__table__.insert().values(
                    round_id=round_id,
                    name=f"Special scoring Race {index} {suffix}",
                    void_reason="official no-contest" if index in void_position_set else None,
                    voided_at=now if index in void_position_set else None,
                    created_at=now,
                    updated_at=now,
                )
            ).inserted_primary_key[0]
            for index in range(1, 4)
        )
        result_ids = tuple(
            connection.execute(
                Win5ResultORM.__table__.insert().values(
                    race_id=race_id,
                    race_entry_id=None,
                    gate_number=gate_number,
                    position=1,
                    created_at=now,
                )
            ).inserted_primary_key[0]
            for index, (race_id, gate_number) in enumerate(zip(race_ids, (3, 7, 1), strict=True), start=1)
            if index not in void_position_set
        )

        submission_ids: list[int] = []
        for index, persona_id in enumerate(persona_ids):
            submission_id = connection.execute(
                Win5SubmissionORM.__table__.insert().values(
                    round_id=round_id,
                    persona_id=persona_id,
                    tier="SPECIAL_WINNER",
                    status="accepted",
                    active_marker=True,
                    version=4 + index,
                    created_at=now,
                    updated_at=now,
                )
            ).inserted_primary_key[0]
            submission_ids.append(submission_id)
            if index == 0:
                gates = (3,) if 2 in void_position_set else (3, 4)
            else:
                gates = (3, 7, 1)
            for race_id, gate_number in zip(race_ids, gates, strict=False):
                connection.execute(
                    Win5SubmissionPickORM.__table__.insert().values(
                        submission_id=submission_id,
                        race_id=race_id,
                        race_entry_id=None,
                        gate_number=gate_number,
                        position=1,
                    )
                )

    return SeededSpecialScoringRound(
        season_id=season_id,
        round_id=round_id,
        race_ids=race_ids,
        result_ids=result_ids,
        persona_ids=persona_ids,
        submission_ids=tuple(submission_ids),
    )


def _cleanup(engine: Engine, seeded: SeededSpecialScoringRound) -> None:
    with engine.begin() as connection:
        connection.execute(
            delete(DiscordPublicationORM).where(
                DiscordPublicationORM.source_kind == "win5_round",
                DiscordPublicationORM.source_id == seeded.round_id,
            )
        )
        event_ids = tuple(
            connection.scalars(select(Win5ScoreEventORM.id).where(Win5ScoreEventORM.round_id == seeded.round_id))
        )
        if event_ids:
            connection.execute(delete(Win5ScoreEventItemORM).where(Win5ScoreEventItemORM.score_event_id.in_(event_ids)))
        connection.execute(delete(Win5ScoreEventORM).where(Win5ScoreEventORM.round_id == seeded.round_id))
        connection.execute(delete(Win5ScoreORM).where(Win5ScoreORM.season_id == seeded.season_id))
        operation_ids = tuple(
            connection.scalars(
                select(Win5OperationORM.operation_id).where(Win5OperationORM.round_id == seeded.round_id)
            )
        )
        if operation_ids:
            connection.execute(delete(PointTransactionORM).where(PointTransactionORM.operation_id.in_(operation_ids)))
            connection.execute(delete(Win5OperationORM).where(Win5OperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(
            delete(Win5SubmissionPickORM).where(Win5SubmissionPickORM.submission_id.in_(seeded.submission_ids))
        )
        connection.execute(delete(Win5SubmissionORM).where(Win5SubmissionORM.id.in_(seeded.submission_ids)))
        connection.execute(delete(Win5ResultORM).where(Win5ResultORM.id.in_(seeded.result_ids)))
        connection.execute(delete(Win5RaceORM).where(Win5RaceORM.id.in_(seeded.race_ids)))
        connection.execute(delete(Win5RoundORM).where(Win5RoundORM.id == seeded.round_id))
        connection.execute(delete(Win5SeasonORM).where(Win5SeasonORM.id == seeded.season_id))
        connection.execute(delete(PersonaORM).where(PersonaORM.id.in_(seeded.persona_ids)))


def test_concurrent_exact_retry_persists_one_special_batch_without_point_writes(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_special_scoring_round(migrated_engine, suffix=suffix)
    idempotency_key = f"special-score-{suffix}"
    commands = compose_win5_special_scoring(DatabaseRuntime.from_engine(migrated_engine))
    command_ = ScoreSpecialWin5Round(
        round_id=seeded.round_id,
        idempotency_key=idempotency_key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id=f"special-score-correlation-{suffix}",
        reason="integration Special score confirmed",
    )
    start = Barrier(2)

    def score_after_barrier() -> object:
        start.wait()
        return commands.score_round(command_)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(score_after_barrier) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]

        assert results[0] == results[1]
        result = results[0]
        assert result.scoring_policy_version == WIN5_SPECIAL_SCORING_POLICY_VERSION
        assert result.void_race_ids == ()
        assert result.season_score_delta == 4
        assert result.top1_score_delta == 4
        assert result.circle_point_reward == 0

        with migrated_engine.connect() as connection:
            assert connection.scalar(select(Win5RoundORM.status).where(Win5RoundORM.id == seeded.round_id)) == "scored"
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(OperationORM)
                    .where(OperationORM.idempotency_key == idempotency_key)
                )
                == 1
            )
            events = connection.execute(
                select(
                    Win5ScoreEventORM.id,
                    Win5ScoreEventORM.persona_id,
                    Win5ScoreEventORM.race_id,
                    Win5ScoreEventORM.exact_count,
                    Win5ScoreEventORM.off_board_count,
                    Win5ScoreEventORM.missing_count,
                    Win5ScoreEventORM.season_score_delta,
                    Win5ScoreEventORM.top1_score_delta,
                    Win5ScoreEventORM.circle_point_reward,
                )
                .where(Win5ScoreEventORM.round_id == seeded.round_id)
                .order_by(Win5ScoreEventORM.persona_id)
            ).all()
            event_ids = tuple(event.id for event in events)
            items = connection.execute(
                select(
                    Win5ScoreEventItemORM.score_event_id,
                    Win5ScoreEventItemORM.race_id,
                    Win5ScoreEventItemORM.position,
                    Win5ScoreEventItemORM.outcome,
                    Win5ScoreEventItemORM.season_score_delta,
                )
                .where(Win5ScoreEventItemORM.score_event_id.in_(event_ids))
                .order_by(Win5ScoreEventItemORM.score_event_id, Win5ScoreEventItemORM.race_id)
            ).all()
            projections = sorted(
                connection.execute(
                    select(Win5ScoreORM.season_score, Win5ScoreORM.top1_score).where(
                        Win5ScoreORM.season_id == seeded.season_id
                    )
                ).all()
            )
            point_write_count = connection.scalar(
                select(func.count())
                .select_from(PointTransactionORM)
                .join(OperationORM, OperationORM.id == PointTransactionORM.operation_id)
                .where(OperationORM.idempotency_key == idempotency_key)
            )
            publications = connection.execute(
                select(
                    DiscordPublicationORM.event_type,
                    DiscordPublicationORM.event_key,
                    DiscordPublicationORM.status,
                    DiscordPublicationORM.payload_json,
                ).where(
                    DiscordPublicationORM.source_kind == "win5_round",
                    DiscordPublicationORM.source_id == seeded.round_id,
                )
            ).all()

        assert len(events) == 2
        assert all(event.race_id is None for event in events)
        assert sorted(
            (
                event.exact_count,
                event.off_board_count,
                event.missing_count,
                event.season_score_delta,
                event.top1_score_delta,
                event.circle_point_reward,
            )
            for event in events
        ) == [(1, 1, 1, 1, 1, 0), (3, 0, 0, 3, 3, 0)]
        assert len(items) == 6
        assert {item.race_id for item in items} == set(seeded.race_ids)
        assert all(item.position == 1 for item in items)
        assert sorted((item.outcome, item.season_score_delta) for item in items) == [
            ("exact", 1),
            ("exact", 1),
            ("exact", 1),
            ("exact", 1),
            ("missing", 0),
            ("off_board", 0),
        ]
        assert projections == [(1, 1), (3, 3)]
        assert point_write_count == 0
        assert len(publications) == 1
        publication = publications[0]
        assert publication.event_type == "win5_round_scored"
        assert publication.event_key == f"win5-round:{seeded.round_id}:scored-result:v1"
        assert publication.status == "awaiting_channel"
        assert publication.payload_json["round"]["type"] == "special"
        assert [race["race_id"] for race in publication.payload_json["results"]] == list(seeded.race_ids)
        assert len(publication.payload_json["hits"]) == 2
        assert publication.payload_json["no_hits"] is False
    finally:
        _cleanup(migrated_engine, seeded)


def test_mixed_void_special_scoring_persists_void_items_and_v2_publication(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_special_scoring_round(migrated_engine, suffix=suffix, void_positions=(2,))
    idempotency_key = f"special-mixed-void-score-{suffix}"
    commands = compose_win5_special_scoring(DatabaseRuntime.from_engine(migrated_engine))
    command_ = ScoreSpecialWin5Round(
        round_id=seeded.round_id,
        idempotency_key=idempotency_key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id=f"special-mixed-void-score-correlation-{suffix}",
        reason="integration mixed-void Special score confirmed",
    )

    try:
        result = commands.score_round(command_)
        retried = commands.score_round(command_)

        assert retried == result
        assert result.scoring_policy_version == WIN5_SPECIAL_VOID_SCORING_POLICY_VERSION
        assert result.void_race_ids == (seeded.race_ids[1],)
        assert result.season_score_delta == 3
        assert result.top1_score_delta == 3
        assert [event.void_count for event in result.events] == [1, 1]

        with migrated_engine.connect() as connection:
            events = connection.execute(
                select(
                    Win5ScoreEventORM.id,
                    Win5ScoreEventORM.exact_count,
                    Win5ScoreEventORM.off_board_count,
                    Win5ScoreEventORM.missing_count,
                    Win5ScoreEventORM.scoring_policy_version,
                )
                .where(Win5ScoreEventORM.round_id == seeded.round_id)
                .order_by(Win5ScoreEventORM.persona_id)
            ).all()
            event_ids = tuple(event.id for event in events)
            items = connection.execute(
                select(
                    Win5ScoreEventItemORM.race_id,
                    Win5ScoreEventItemORM.submission_pick_id,
                    Win5ScoreEventItemORM.matched_result_id,
                    Win5ScoreEventItemORM.outcome,
                    Win5ScoreEventItemORM.season_score_delta,
                )
                .where(Win5ScoreEventItemORM.score_event_id.in_(event_ids))
                .order_by(Win5ScoreEventItemORM.score_event_id, Win5ScoreEventItemORM.race_id)
            ).all()
            operation_count = connection.scalar(
                select(func.count()).select_from(OperationORM).where(OperationORM.idempotency_key == idempotency_key)
            )
            point_write_count = connection.scalar(
                select(func.count())
                .select_from(PointTransactionORM)
                .join(OperationORM, OperationORM.id == PointTransactionORM.operation_id)
                .where(OperationORM.idempotency_key == idempotency_key)
            )
            (publication,) = connection.execute(
                select(DiscordPublicationORM.payload_json).where(
                    DiscordPublicationORM.source_kind == "win5_round",
                    DiscordPublicationORM.source_id == seeded.round_id,
                )
            ).one()

        assert sorted((event.exact_count, event.off_board_count, event.missing_count) for event in events) == [
            (1, 0, 1),
            (2, 0, 0),
        ]
        assert all(event.scoring_policy_version == WIN5_SPECIAL_VOID_SCORING_POLICY_VERSION for event in events)
        void_items = [item for item in items if item.outcome == "void"]
        assert len(items) == 6
        assert len(void_items) == 2
        assert all(item.race_id == seeded.race_ids[1] for item in void_items)
        assert sum(item.submission_pick_id is None for item in void_items) == 1
        assert sum(item.submission_pick_id is not None for item in void_items) == 1
        assert all(item.matched_result_id is None and item.season_score_delta == 0 for item in void_items)
        assert operation_count == 1
        assert point_write_count == 0
        assert publication["schema_version"] == 2
        assert publication["results"][1] == {
            "race_id": seeded.race_ids[1],
            "race_name": f"Special scoring Race 2 {suffix}",
            "placements": [],
            "void_reason": "official no-contest",
        }
    finally:
        _cleanup(migrated_engine, seeded)
