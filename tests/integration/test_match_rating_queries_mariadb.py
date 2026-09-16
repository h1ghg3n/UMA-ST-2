"""MariaDB evidence for current GameAccount Rating standings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine

from uma_st2.compose import compose_match_rating_queries
from uma_st2.infrastructure.database import DatabaseRuntime
from uma_st2.infrastructure.database.orm import GameAccountORM, PersonaORM, RatingORM

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True)
class SeededRatings:
    persona_ids: tuple[str, ...]
    game_account_ids: tuple[int, ...]


def _seed(engine: Engine) -> SeededRatings:
    now = datetime.now(UTC).replace(tzinfo=None)
    suffix = uuid4().hex
    names_and_ratings = (
        ("First", Decimal("900000000.000000000000000000")),
        ("Target precise high", Decimal("899999999.000000000000000001")),
        ("Precise low", Decimal("899999999.000000000000000000")),
        ("Fourth", Decimal("899999998.000000000000000000")),
        ("Target 100%_literal", Decimal("899999997.000000000000000000")),
        ("Target tie one", Decimal("899999996.000000000000000000")),
        ("Tie two", Decimal("899999996.000000000000000000")),
        ("Eighth", Decimal("899999995.000000000000000000")),
    )
    persona_ids: list[str] = []
    account_ids: list[int] = []
    with engine.begin() as connection:
        for index, (name, rating) in enumerate(names_and_ratings, start=1):
            persona_id = str(uuid4())
            persona_ids.append(persona_id)
            connection.execute(
                PersonaORM.__table__.insert().values(
                    id=persona_id,
                    display_name=f"{name} {suffix}",
                    status="normal",
                    created_at=now,
                    updated_at=now,
                )
            )
            account_id = connection.execute(
                GameAccountORM.__table__.insert().values(
                    persona_id=persona_id,
                    game_region="KR",
                    uma_pid=f"r{suffix[:20]}{index}",
                    nickname=f"Rating account {index} {suffix}",
                    affiliation=None,
                    created_at=now,
                    updated_at=now,
                )
            ).inserted_primary_key[0]
            account_ids.append(account_id)
            connection.execute(
                RatingORM.__table__.insert().values(
                    game_account_id=account_id,
                    rating=rating,
                    updated_at=now,
                )
            )
    return SeededRatings(tuple(persona_ids), tuple(account_ids))


def _cleanup(engine: Engine, seeded: SeededRatings) -> None:
    with engine.begin() as connection:
        connection.execute(delete(RatingORM).where(RatingORM.game_account_id.in_(seeded.game_account_ids)))
        connection.execute(delete(GameAccountORM).where(GameAccountORM.id.in_(seeded.game_account_ids)))
        connection.execute(delete(PersonaORM).where(PersonaORM.id.in_(seeded.persona_ids)))


def test_mariadb_rating_rank_is_global_full_precision_and_filtered_after_window(
    migrated_engine: Engine,
) -> None:
    seeded = _seed(migrated_engine)
    runtime = DatabaseRuntime.from_engine(migrated_engine)
    try:
        queries = compose_match_rating_queries(runtime)
        with migrated_engine.connect() as connection:
            before = connection.scalar(
                select(func.count())
                .select_from(RatingORM)
                .where(RatingORM.game_account_id.in_(seeded.game_account_ids))
            )

        all_rows = tuple(row for row in queries.list_ratings() if row.game_account_id in seeded.game_account_ids)
        assert [row.competition_rank for row in all_rows] == [1, 2, 3, 4, 5, 6, 6, 8]
        assert all_rows[1].current_rating > all_rows[2].current_rating

        rank_rows = tuple(row for row in queries.list_ratings(rank=3) if row.game_account_id in seeded.game_account_ids)
        assert [row.competition_rank for row in rank_rows] == [3, 4, 5, 6, 6, 8]

        persona_rows = tuple(
            row for row in queries.list_ratings(persona="  Target  ") if row.game_account_id in seeded.game_account_ids
        )
        assert [row.competition_rank for row in persona_rows] == [2, 5, 6]

        literal_rows = tuple(
            row for row in queries.list_ratings(persona="100%_") if row.game_account_id in seeded.game_account_ids
        )
        assert [row.competition_rank for row in literal_rows] == [5]

        with migrated_engine.connect() as connection:
            after = connection.scalar(
                select(func.count())
                .select_from(RatingORM)
                .where(RatingORM.game_account_id.in_(seeded.game_account_ids))
            )
        assert before == after == len(seeded.game_account_ids)
    finally:
        runtime.dispose()
        _cleanup(migrated_engine, seeded)
