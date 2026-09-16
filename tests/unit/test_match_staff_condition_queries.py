"""Staff Match-condition projection tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.execution import QueryRunner
from uma_st2.application.match import MatchConditionTarget, MatchStaffConditionQueries
from uma_st2.domain.match import MatchSourceKind, MatchStatus


def _target(match_id: int) -> MatchConditionTarget:
    return MatchConditionTarget(
        match_id=match_id,
        name=f"Match {match_id}",
        source_kind=MatchSourceKind.NATIVE_V2,
        status=MatchStatus.SCHEDULED,
        scheduled_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        condition=None,
        condition_version=None,
    )


class RecordingRepository:
    def __init__(self) -> None:
        self.search_calls: list[tuple[str, int]] = []
        self.get_calls: list[int] = []

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchConditionTarget, ...]:
        self.search_calls.append((search, limit))
        return (_target(1), _target(2))

    def get_target(self, *, match_id: int) -> MatchConditionTarget | None:
        self.get_calls.append(match_id)
        return _target(match_id)


@dataclass
class RecordingUnitOfWork:
    match_staff_condition_queries: RecordingRepository

    def __enter__(self) -> RecordingUnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return False

    def commit(self) -> None:
        raise AssertionError("QueryRunner must not commit")

    def rollback(self) -> None:
        pass


def _queries(repository: RecordingRepository) -> MatchStaffConditionQueries:
    return MatchStaffConditionQueries(QueryRunner(lambda: RecordingUnitOfWork(repository)))


def test_search_and_exact_target_use_fresh_closed_query_uows() -> None:
    repository = RecordingRepository()
    queries = _queries(repository)

    results = queries.search_targets(search="  정기전  ", limit=25)
    exact = queries.get_target(match_id=7)

    assert [target.match_id for target in results] == [1, 2]
    assert exact is not None and exact.match_id == 7
    assert repository.search_calls == [("정기전", 25)]
    assert repository.get_calls == [7]


@pytest.mark.parametrize("limit", (0, 26, True))
def test_search_limit_is_bounded(limit: object) -> None:
    with pytest.raises(ValueError):
        _queries(RecordingRepository()).search_targets(limit=limit)  # type: ignore[arg-type]
