"""Staff WIN5 Season query application and SQLAlchemy tests."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine

from uma_st2.application.win5 import (
    Win5SeasonAction,
    Win5StaffSeasonUnavailableError,
)
from uma_st2.compose import compose_win5_staff_season_queries
from uma_st2.domain.win5 import Win5RoundStatus, Win5RoundType
from uma_st2.infrastructure.database import Base, DatabaseRuntime
from uma_st2.infrastructure.database.orm import Win5RoundORM, Win5SeasonORM

NOW = datetime(2026, 8, 27, 9, 0)


def _season_row(season_id: int, *, name: str, status: str, offset: int) -> dict[str, object]:
    created_at = NOW + timedelta(minutes=offset)
    return {
        "id": season_id,
        "name": name,
        "status": status,
        "active_marker": True if status == "active" else None,
        "starts_at": None,
        "ends_at": None,
        "created_at": created_at,
        "updated_at": created_at,
    }


def _round_row(round_id: int, *, season_id: int, status: Win5RoundStatus) -> dict[str, object]:
    return {
        "id": round_id,
        "season_id": season_id,
        "type": Win5RoundType.NORMAL.value,
        "status": status.value,
        "name": f"Round {round_id}",
        "opens_at": None,
        "closes_at": None,
        "created_at": NOW,
        "updated_at": NOW,
    }


def test_composed_queries_filter_each_action_by_current_round_matrix() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            connection.execute(
                Win5SeasonORM.__table__.insert(),
                [
                    _season_row(1, name="draft empty", status="draft", offset=0),
                    _season_row(2, name="draft setup", status="draft", offset=1),
                    _season_row(3, name="active terminal", status="active", offset=2),
                    _season_row(4, name="closed", status="closed", offset=3),
                ],
            )
            connection.execute(
                Win5RoundORM.__table__.insert(),
                [
                    _round_row(21, season_id=2, status=Win5RoundStatus.SETUP),
                    _round_row(31, season_id=3, status=Win5RoundStatus.SCORED),
                    _round_row(32, season_id=3, status=Win5RoundStatus.CANCELLED),
                ],
            )
        queries = compose_win5_staff_season_queries(runtime)

        assert tuple(item.id for item in queries.list_season_targets(action=Win5SeasonAction.ACTIVATE).choices) == (
            1,
            2,
        )
        assert tuple(item.id for item in queries.list_season_targets(action=Win5SeasonAction.CANCEL).choices) == (1,)
        assert tuple(item.id for item in queries.list_season_targets(action=Win5SeasonAction.CLOSE).choices) == (3,)
        assert tuple(item.id for item in queries.list_season_targets(action=Win5SeasonAction.EDIT).choices) == (
            1,
            2,
            3,
            4,
        )
        assert (
            queries.get_season_target(
                season_id=3,
                expected_action=Win5SeasonAction.CLOSE,
            ).rounds.scored
            == 1
        )
    finally:
        runtime.dispose()


def test_fresh_target_rejects_action_that_became_ineligible() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            connection.execute(
                Win5SeasonORM.__table__.insert(),
                _season_row(1, name="draft setup", status="draft", offset=0),
            )
            connection.execute(
                Win5RoundORM.__table__.insert(),
                _round_row(11, season_id=1, status=Win5RoundStatus.SETUP),
            )

        with pytest.raises(Win5StaffSeasonUnavailableError):
            compose_win5_staff_season_queries(runtime).get_season_target(
                season_id=1,
                expected_action=Win5SeasonAction.CANCEL,
            )
    finally:
        runtime.dispose()
