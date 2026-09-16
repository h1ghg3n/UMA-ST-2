"""Application query tests for current Room Match Rating standings."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import TracebackType

import pytest

from uma_st2.application.execution import QueryRunner
from uma_st2.application.rating import (
    MAX_RATING_STANDING_ROWS,
    MatchRatingFilterConflictError,
    MatchRatingInvalidSourceError,
    MatchRatingQueries,
    MatchRatingQueryError,
    MatchRatingResultTooLargeError,
    MatchRatingStanding,
)


def _standing(index: int = 1, *, rank: int = 1) -> MatchRatingStanding:
    return MatchRatingStanding(
        game_account_id=index,
        current_owner_persona_id=f"00000000-0000-0000-0000-{index:012d}",
        current_owner_display_name=f"Persona {index}",
        current_rating=Decimal("1500.125000000000000000"),
        competition_rank=rank,
    )


class RecordingRepository:
    def __init__(self, rows: object = None, *, error: Exception | None = None) -> None:
        self.rows = (_standing(),) if rows is None else rows
        self.error = error
        self.calls: list[dict[str, object]] = []

    def list_standings(self, **kwargs: object) -> tuple[MatchRatingStanding, ...]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.rows  # type: ignore[return-value]


@dataclass
class RecordingUnitOfWork:
    match_rating_queries: RecordingRepository
    commit_count: int = 0
    rollback_count: int = 0
    entered: bool = False
    exited: bool = False

    def __enter__(self) -> RecordingUnitOfWork:
        self.entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.exited = True
        return False

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


class RecordingFactory:
    def __init__(self, repository: RecordingRepository) -> None:
        self.repository = repository
        self.created: list[RecordingUnitOfWork] = []

    def __call__(self) -> RecordingUnitOfWork:
        unit_of_work = RecordingUnitOfWork(self.repository)
        self.created.append(unit_of_work)
        return unit_of_work


def _queries(repository: RecordingRepository) -> tuple[MatchRatingQueries, RecordingFactory]:
    factory = RecordingFactory(repository)
    return MatchRatingQueries(QueryRunner(factory)), factory


def test_no_filter_reads_global_ranks_one_through_fifty_without_commit() -> None:
    repository = RecordingRepository(rows=(_standing(1, rank=1), _standing(2, rank=50)))
    queries, factory = _queries(repository)

    assert tuple(row.competition_rank for row in queries.list_ratings()) == (1, 50)
    assert repository.calls == [
        {
            "rank_start": 1,
            "rank_end": 50,
            "persona_query": None,
            "limit": MAX_RATING_STANDING_ROWS + 1,
        }
    ]
    unit_of_work = factory.created[0]
    assert unit_of_work.entered and unit_of_work.exited
    assert unit_of_work.commit_count == 0
    assert unit_of_work.rollback_count == 1


def test_standing_accepts_full_numeric_30_18_range() -> None:
    standing = MatchRatingStanding(
        game_account_id=1,
        current_owner_persona_id=None,
        current_owner_display_name=None,
        current_rating=Decimal("999999999999.999999999999999999"),
        competition_rank=1,
    )

    assert standing.current_rating == Decimal("999999999999.999999999999999999")


@pytest.mark.parametrize(
    ("owner_id", "owner_name"),
    (("00000000-0000-0000-0000-000000000001", None), (None, "Persona")),
)
def test_standing_rejects_partial_current_owner_metadata(owner_id: str | None, owner_name: str | None) -> None:
    with pytest.raises(MatchRatingQueryError, match="present together"):
        MatchRatingStanding(
            game_account_id=1,
            current_owner_persona_id=owner_id,
            current_owner_display_name=owner_name,
            current_rating=Decimal("1500.000000000000000000"),
            competition_rank=1,
        )


def test_rank_filter_uses_requested_ten_rank_interval() -> None:
    repository = RecordingRepository(rows=(_standing(rank=12),))
    queries, _ = _queries(repository)

    assert queries.list_ratings(rank=12) == (_standing(rank=12),)
    assert repository.calls[0] == {
        "rank_start": 12,
        "rank_end": 21,
        "persona_query": None,
        "limit": MAX_RATING_STANDING_ROWS + 1,
    }


def test_persona_filter_is_trimmed_and_preserves_repository_global_rank() -> None:
    repository = RecordingRepository(rows=(_standing(rank=37),))
    queries, _ = _queries(repository)

    result = queries.list_ratings(persona="  Persona  ")

    assert result[0].competition_rank == 37
    assert repository.calls[0] == {
        "rank_start": None,
        "rank_end": None,
        "persona_query": "Persona",
        "limit": MAX_RATING_STANDING_ROWS + 1,
    }


def test_mutually_exclusive_filters_fail_before_opening_uow() -> None:
    queries, factory = _queries(RecordingRepository())

    with pytest.raises(MatchRatingFilterConflictError):
        queries.list_ratings(rank=1, persona="Persona")

    assert factory.created == []


@pytest.mark.parametrize(
    ("rank", "persona"),
    ((0, None), (True, None), (None, "  "), (None, "x" * 101)),
)
def test_invalid_filter_fails_before_opening_uow(rank: object, persona: object) -> None:
    queries, factory = _queries(RecordingRepository())

    with pytest.raises(MatchRatingQueryError):
        queries.list_ratings(rank=rank, persona=persona)  # type: ignore[arg-type]

    assert factory.created == []


def test_capacity_overflow_fails_instead_of_truncating() -> None:
    rows = tuple(_standing(index, rank=1) for index in range(1, MAX_RATING_STANDING_ROWS + 2))
    queries, factory = _queries(RecordingRepository(rows=rows))

    with pytest.raises(MatchRatingResultTooLargeError):
        queries.list_ratings()

    assert factory.created[0].rollback_count == 0


@pytest.mark.parametrize("rows", ([_standing()], (object(),)))
def test_malformed_repository_shape_maps_to_invalid_source(rows: object) -> None:
    repository = RecordingRepository(rows=rows)
    queries, _ = _queries(repository)

    with pytest.raises(MatchRatingInvalidSourceError):
        queries.list_ratings()


def test_malformed_repository_value_maps_to_invalid_source() -> None:
    queries, _ = _queries(RecordingRepository(error=ValueError("bad rank")))

    with pytest.raises(MatchRatingInvalidSourceError):
        queries.list_ratings()
