"""Focused tests for the persisted WIN5 Season singleton marker."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from uma_st2.domain.win5 import Win5SeasonStatus
from uma_st2.infrastructure.database import Base
from uma_st2.infrastructure.database.orm import Win5RoundORM, Win5SeasonORM
from uma_st2.infrastructure.database.win5_member_commands import SqlAlchemyWin5MemberCommandRepository
from uma_st2.infrastructure.database.win5_normal_results import SqlAlchemyWin5NormalResultRepository
from uma_st2.infrastructure.database.win5_normal_scoring import SqlAlchemyWin5NormalScoringRepository
from uma_st2.infrastructure.database.win5_round_creation import SqlAlchemyWin5RoundCreationRepository
from uma_st2.infrastructure.database.win5_round_deletion import SqlAlchemyWin5SetupRoundDeletionRepository
from uma_st2.infrastructure.database.win5_round_lifecycle import SqlAlchemyWin5RoundLifecycleRepository
from uma_st2.infrastructure.database.win5_season_marker import (
    validate_win5_season_marker,
    win5_season_active_marker,
)
from uma_st2.infrastructure.database.win5_special_results import SqlAlchemyWin5SpecialResultRepository
from uma_st2.infrastructure.database.win5_special_scoring import SqlAlchemyWin5SpecialScoringRepository
from uma_st2.infrastructure.database.win5_special_voids import SqlAlchemyWin5SpecialVoidRepository

NOW = datetime(2026, 8, 27)


@pytest.mark.parametrize(
    ("status", "expected_marker"),
    [
        (Win5SeasonStatus.DRAFT, None),
        (Win5SeasonStatus.ACTIVE, True),
        (Win5SeasonStatus.CLOSED, None),
        (Win5SeasonStatus.CANCELLED, None),
    ],
)
def test_marker_is_true_only_for_active_season(
    status: Win5SeasonStatus,
    expected_marker: bool | None,
) -> None:
    assert win5_season_active_marker(status) is expected_marker
    assert validate_win5_season_marker(status=status, active_marker=expected_marker) == status


@pytest.mark.parametrize(
    ("status", "active_marker"),
    [
        (Win5SeasonStatus.ACTIVE, None),
        (Win5SeasonStatus.DRAFT, True),
        (Win5SeasonStatus.CLOSED, True),
        (Win5SeasonStatus.CANCELLED, True),
    ],
)
def test_marker_validation_fails_closed_on_status_drift(
    status: Win5SeasonStatus,
    active_marker: bool | None,
) -> None:
    with pytest.raises(ValueError, match="status and active_marker"):
        validate_win5_season_marker(status=status, active_marker=active_marker)


@pytest.mark.parametrize(
    ("repository_type", "method_name", "identifier_name"),
    [
        (SqlAlchemyWin5MemberCommandRepository, "lock_round", "round_id"),
        (SqlAlchemyWin5NormalResultRepository, "lock_round", "round_id"),
        (SqlAlchemyWin5NormalScoringRepository, "lock_round", "round_id"),
        (SqlAlchemyWin5SpecialResultRepository, "lock_round", "round_id"),
        (SqlAlchemyWin5SpecialScoringRepository, "lock_round", "round_id"),
        (SqlAlchemyWin5SpecialVoidRepository, "lock_round", "round_id"),
        (SqlAlchemyWin5RoundCreationRepository, "lock_season", "season_id"),
        (SqlAlchemyWin5SetupRoundDeletionRepository, "lock_season", "season_id"),
        (SqlAlchemyWin5RoundLifecycleRepository, "lock_season", "season_id"),
    ],
)
def test_win5_mutation_repositories_reject_inconsistent_season_source(
    repository_type: type[object],
    method_name: str,
    identifier_name: str,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with engine.begin() as connection:
            connection.execute(
                Win5SeasonORM.__table__.insert(),
                {
                    "id": 7,
                    "name": "Invalid active Season",
                    "status": "active",
                    "active_marker": None,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            )
            connection.execute(
                Win5RoundORM.__table__.insert(),
                {
                    "id": 11,
                    "season_id": 7,
                    "type": "special",
                    "status": "closed",
                    "name": "Source guard Round",
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            )

        with Session(engine) as session:
            repository = repository_type(session)
            operation = getattr(repository, method_name)
            identifier = 11 if identifier_name == "round_id" else 7
            with pytest.raises(ValueError, match="status and active_marker"):
                operation(**{identifier_name: identifier})
    finally:
        engine.dispose()
