"""MariaDB transaction evidence for Normal WIN5 scoring and rewards."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine

from uma_st2.application.win5 import ScoreNormalWin5Round, Win5NormalScoringWalletUnavailableError
from uma_st2.compose import compose_win5_normal_scoring
from uma_st2.infrastructure.database import DatabaseRuntime
from uma_st2.infrastructure.database.orm import (
    CirclePointORM,
    DiscordPublicationORM,
    OperationORM,
    PersonaORM,
    PointTransactionORM,
    Win5OperationORM,
    Win5RaceEntryORM,
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
class SeededNormalRound:
    season_id: int
    round_id: int
    race_id: int
    persona_ids: tuple[str, ...]
    entry_ids: tuple[int, ...]
    result_ids: tuple[int, ...]
    submission_ids: tuple[int, ...]
    pick_ids: tuple[int, ...]


def _seed_normal_round(
    engine: Engine,
    *,
    with_wallets: tuple[bool, ...],
    suffix: str,
) -> SeededNormalRound:
    now = datetime.now(UTC).replace(tzinfo=None)
    persona_ids = tuple(str(uuid4()) for _ in with_wallets)
    with engine.begin() as connection:
        for index, (persona_id, with_wallet) in enumerate(zip(persona_ids, with_wallets, strict=True), start=1):
            connection.execute(
                PersonaORM.__table__.insert().values(
                    id=persona_id,
                    display_name=f"WIN5 scoring persona {index}",
                    status="normal",
                    created_at=now,
                    updated_at=now,
                )
            )
            if with_wallet:
                connection.execute(
                    CirclePointORM.__table__.insert().values(
                        persona_id=persona_id,
                        balance=index * 100,
                        updated_at=now,
                    )
                )

        season_id = connection.execute(
            Win5SeasonORM.__table__.insert().values(
                name=f"Normal scoring Season {suffix}",
                status="active",
                active_marker=True,
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        round_id = connection.execute(
            Win5RoundORM.__table__.insert().values(
                season_id=season_id,
                type="normal",
                status="closed",
                name=f"Normal scoring Round {suffix}",
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        race_id = connection.execute(
            Win5RaceORM.__table__.insert().values(
                round_id=round_id,
                name=f"Normal scoring Race {suffix}",
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        entry_ids = tuple(
            connection.execute(
                Win5RaceEntryORM.__table__.insert().values(
                    race_id=race_id,
                    gate_number=position,
                    name=f"entry-{position}-{suffix}",
                    created_at=now,
                    updated_at=now,
                )
            ).inserted_primary_key[0]
            for position in range(1, 7)
        )
        result_ids = tuple(
            connection.execute(
                Win5ResultORM.__table__.insert().values(
                    race_id=race_id,
                    race_entry_id=entry_ids[position - 1],
                    gate_number=None,
                    position=position,
                    created_at=now,
                )
            ).inserted_primary_key[0]
            for position in range(1, 6)
        )

        submission_ids: list[int] = []
        pick_ids: list[int] = []
        for index, persona_id in enumerate(persona_ids):
            tier = "TOP5" if index == 0 else "TOP1"
            submission_id = connection.execute(
                Win5SubmissionORM.__table__.insert().values(
                    round_id=round_id,
                    persona_id=persona_id,
                    tier=tier,
                    status="accepted",
                    active_marker=True,
                    version=3 + index,
                    created_at=now,
                    updated_at=now,
                )
            ).inserted_primary_key[0]
            submission_ids.append(submission_id)
            desired_picks = (
                (
                    (1, entry_ids[0]),
                    (2, entry_ids[2]),
                    (3, entry_ids[5]),
                )
                if index == 0
                else ((1, entry_ids[0]),)
            )
            for position, entry_id in desired_picks:
                pick_ids.append(
                    connection.execute(
                        Win5SubmissionPickORM.__table__.insert().values(
                            submission_id=submission_id,
                            race_id=race_id,
                            race_entry_id=entry_id,
                            gate_number=None,
                            position=position,
                        )
                    ).inserted_primary_key[0]
                )

    return SeededNormalRound(
        season_id=season_id,
        round_id=round_id,
        race_id=race_id,
        persona_ids=persona_ids,
        entry_ids=entry_ids,
        result_ids=result_ids,
        submission_ids=tuple(submission_ids),
        pick_ids=tuple(pick_ids),
    )


def _cleanup(engine: Engine, seeded: SeededNormalRound, *, idempotency_key: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            delete(DiscordPublicationORM).where(
                DiscordPublicationORM.source_kind == "win5_round",
                DiscordPublicationORM.source_id == seeded.round_id,
            )
        )
        operation_ids = tuple(
            connection.scalars(select(OperationORM.id).where(OperationORM.idempotency_key == idempotency_key))
        )
        event_ids = tuple(
            connection.scalars(select(Win5ScoreEventORM.id).where(Win5ScoreEventORM.round_id == seeded.round_id))
        )
        if event_ids:
            connection.execute(delete(Win5ScoreEventItemORM).where(Win5ScoreEventItemORM.score_event_id.in_(event_ids)))
        connection.execute(delete(Win5ScoreEventORM).where(Win5ScoreEventORM.round_id == seeded.round_id))
        connection.execute(delete(Win5ScoreORM).where(Win5ScoreORM.season_id == seeded.season_id))
        if operation_ids:
            connection.execute(delete(PointTransactionORM).where(PointTransactionORM.operation_id.in_(operation_ids)))
            connection.execute(delete(Win5OperationORM).where(Win5OperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(
            delete(Win5SubmissionPickORM).where(Win5SubmissionPickORM.submission_id.in_(seeded.submission_ids))
        )
        connection.execute(delete(Win5SubmissionORM).where(Win5SubmissionORM.id.in_(seeded.submission_ids)))
        connection.execute(delete(Win5ResultORM).where(Win5ResultORM.id.in_(seeded.result_ids)))
        connection.execute(delete(Win5RaceEntryORM).where(Win5RaceEntryORM.id.in_(seeded.entry_ids)))
        connection.execute(delete(Win5RaceORM).where(Win5RaceORM.id == seeded.race_id))
        connection.execute(delete(Win5RoundORM).where(Win5RoundORM.id == seeded.round_id))
        connection.execute(delete(Win5SeasonORM).where(Win5SeasonORM.id == seeded.season_id))
        connection.execute(delete(CirclePointORM).where(CirclePointORM.persona_id.in_(seeded.persona_ids)))
        connection.execute(delete(PersonaORM).where(PersonaORM.id.in_(seeded.persona_ids)))


def test_concurrent_exact_retry_persists_one_scoring_batch_and_reward_set(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    idempotency_key = f"normal-score-{suffix}"
    seeded = _seed_normal_round(migrated_engine, with_wallets=(True, True), suffix=suffix)
    commands = compose_win5_normal_scoring(DatabaseRuntime.from_engine(migrated_engine))
    command_ = ScoreNormalWin5Round(
        round_id=seeded.round_id,
        idempotency_key=idempotency_key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id=f"normal-score-correlation-{suffix}",
        reason="integration result confirmed",
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
        assert result.season_score_delta == 7
        assert result.top1_score_delta == 3
        assert result.circle_point_reward == 20

        with migrated_engine.connect() as connection:
            round_status = connection.scalar(select(Win5RoundORM.status).where(Win5RoundORM.id == seeded.round_id))
            operation_count = connection.scalar(
                select(func.count()).select_from(OperationORM).where(OperationORM.idempotency_key == idempotency_key)
            )
            events = connection.execute(
                select(
                    Win5ScoreEventORM.persona_id,
                    Win5ScoreEventORM.exact_count,
                    Win5ScoreEventORM.wrong_position_count,
                    Win5ScoreEventORM.off_board_count,
                    Win5ScoreEventORM.missing_count,
                    Win5ScoreEventORM.season_score_delta,
                    Win5ScoreEventORM.top1_score_delta,
                    Win5ScoreEventORM.circle_point_reward,
                )
                .where(Win5ScoreEventORM.round_id == seeded.round_id)
                .order_by(Win5ScoreEventORM.persona_id)
            ).all()
            event_ids = tuple(
                connection.scalars(select(Win5ScoreEventORM.id).where(Win5ScoreEventORM.round_id == seeded.round_id))
            )
            item_count = connection.scalar(
                select(func.count())
                .select_from(Win5ScoreEventItemORM)
                .where(Win5ScoreEventItemORM.score_event_id.in_(event_ids))
            )
            projections = {
                row.persona_id: (row.season_score, row.top1_score)
                for row in connection.execute(
                    select(
                        Win5ScoreORM.persona_id,
                        Win5ScoreORM.season_score,
                        Win5ScoreORM.top1_score,
                    ).where(Win5ScoreORM.season_id == seeded.season_id)
                )
            }
            wallets = {
                row.persona_id: row.balance
                for row in connection.execute(
                    select(CirclePointORM.persona_id, CirclePointORM.balance).where(
                        CirclePointORM.persona_id.in_(seeded.persona_ids)
                    )
                )
            }
            rewards = connection.execute(
                select(PointTransactionORM.persona_id, PointTransactionORM.action, PointTransactionORM.amount)
                .join(OperationORM, OperationORM.id == PointTransactionORM.operation_id)
                .where(OperationORM.idempotency_key == idempotency_key)
                .order_by(PointTransactionORM.persona_id)
            ).all()
            publications = connection.execute(
                select(
                    DiscordPublicationORM.event_type,
                    DiscordPublicationORM.event_key,
                    DiscordPublicationORM.source_kind,
                    DiscordPublicationORM.source_id,
                    DiscordPublicationORM.status,
                    DiscordPublicationORM.target_channel_id,
                    DiscordPublicationORM.payload_json,
                    DiscordPublicationORM.payload_fingerprint,
                ).where(
                    DiscordPublicationORM.source_kind == "win5_round",
                    DiscordPublicationORM.source_id == seeded.round_id,
                )
            ).all()

        expected_personas = tuple(sorted(seeded.persona_ids))
        assert round_status == "scored"
        assert operation_count == 1
        assert [event.persona_id for event in events] == list(expected_personas)
        assert sorted(
            (
                event.exact_count,
                event.wrong_position_count,
                event.off_board_count,
                event.missing_count,
                event.season_score_delta,
                event.top1_score_delta,
                event.circle_point_reward,
            )
            for event in events
        ) == [(1, 0, 0, 0, 3, 3, 10), (1, 1, 1, 2, 4, 0, 10)]
        assert item_count == 6
        assert sorted(projections.values()) == [(3, 3), (4, 0)]
        assert sorted(wallets.values()) == [110, 210]
        assert rewards == [
            (expected_personas[0], "win5_reward", 10),
            (expected_personas[1], "win5_reward", 10),
        ]
        assert len(publications) == 1
        publication = publications[0]
        assert publication.event_type == "win5_round_scored"
        assert publication.event_key == f"win5-round:{seeded.round_id}:scored-result:v1"
        assert publication.source_kind == "win5_round"
        assert publication.source_id == seeded.round_id
        assert publication.status == "awaiting_channel"
        assert publication.target_channel_id is None
        assert publication.payload_json["round"]["id"] == seeded.round_id
        assert publication.payload_json["round"]["type"] == "normal"
        assert publication.payload_json["no_hits"] is False
        assert len(publication.payload_json["hits"]) == 2
        assert len(publication.payload_fingerprint) == 64
    finally:
        _cleanup(migrated_engine, seeded, idempotency_key=idempotency_key)


def test_missing_reward_wallet_rolls_back_every_scoring_write(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    idempotency_key = f"normal-score-missing-wallet-{suffix}"
    seeded = _seed_normal_round(migrated_engine, with_wallets=(False,), suffix=suffix)
    commands = compose_win5_normal_scoring(DatabaseRuntime.from_engine(migrated_engine))

    try:
        with pytest.raises(Win5NormalScoringWalletUnavailableError):
            commands.score_round(
                ScoreNormalWin5Round(
                    round_id=seeded.round_id,
                    idempotency_key=idempotency_key,
                    actor_discord_user_id="123456789",
                    guild_id="987654321",
                )
            )

        with migrated_engine.connect() as connection:
            assert connection.scalar(select(Win5RoundORM.status).where(Win5RoundORM.id == seeded.round_id)) == "closed"
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(OperationORM)
                    .where(OperationORM.idempotency_key == idempotency_key)
                )
                == 0
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(DiscordPublicationORM)
                    .where(
                        DiscordPublicationORM.source_kind == "win5_round",
                        DiscordPublicationORM.source_id == seeded.round_id,
                    )
                )
                == 0
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(Win5ScoreEventORM)
                    .where(Win5ScoreEventORM.round_id == seeded.round_id)
                )
                == 0
            )
            assert (
                connection.scalar(
                    select(func.count()).select_from(Win5ScoreORM).where(Win5ScoreORM.season_id == seeded.season_id)
                )
                == 0
            )
    finally:
        _cleanup(migrated_engine, seeded, idempotency_key=idempotency_key)
