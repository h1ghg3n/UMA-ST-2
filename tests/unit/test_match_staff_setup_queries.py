"""Read-only Application boundary tests for editable Match setup targets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.execution import QueryRunner
from uma_st2.application.match import (
    MatchConditionRecord,
    MatchConditionValues,
    MatchCreationCourse,
    MatchSetupEditorTarget,
    MatchSetupTarget,
    MatchStaffSetupInvalidSourceError,
    MatchStaffSetupQueries,
)
from uma_st2.domain.match import (
    MatchDirection,
    MatchGrade,
    MatchSeason,
    MatchSourceKind,
    MatchStatus,
    MatchSurface,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
    StadiumCourseLayout,
)

NOW = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)


def _target() -> MatchSetupEditorTarget:
    return MatchSetupEditorTarget(
        setup=MatchSetupTarget(
            match_id=71,
            name="제12회 정기전",
            description=None,
            source_kind=MatchSourceKind.NATIVE_V2,
            grade=MatchGrade.G1,
            course=MatchCreationCourse(
                id=21,
                stadium_id=2,
                stadium_name="나카야마",
                surface=MatchSurface.TURF,
                distance=2500,
                direction=MatchDirection.RIGHT,
                layout=StadiumCourseLayout.OUTER_TO_INNER,
            ),
            scheduled_at=NOW,
            status=MatchStatus.SCHEDULED,
            condition=MatchConditionRecord(
                values=MatchConditionValues(
                    season=MatchSeason.AUTUMN,
                    weather=MatchWeather.SUNNY,
                    time_of_day=MatchTimeOfDay.DAY,
                    track_condition=MatchTrackCondition.FIRM,
                ),
                created_at=NOW,
                updated_at=NOW,
            ),
            updated_at=NOW,
            setup_version=5,
        ),
        entry_count=3,
    )


class RecordingRepository:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.search_calls: list[tuple[str, int]] = []
        self.get_calls: list[int] = []

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchSetupEditorTarget, ...]:
        self.search_calls.append((search, limit))
        if self.error is not None:
            raise self.error
        return (_target(),)

    def get_target(self, *, match_id: int) -> MatchSetupEditorTarget | None:
        self.get_calls.append(match_id)
        if self.error is not None:
            raise self.error
        return _target() if match_id == 71 else None


@dataclass
class RecordingUnitOfWork:
    match_staff_setup_queries: RecordingRepository
    commits: int = 0
    rollbacks: int = 0
    exited: bool = False

    def __enter__(self) -> RecordingUnitOfWork:
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
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class RecordingFactory:
    def __init__(self, repository: RecordingRepository) -> None:
        self.repository = repository
        self.created: list[RecordingUnitOfWork] = []

    def __call__(self) -> RecordingUnitOfWork:
        unit_of_work = RecordingUnitOfWork(self.repository)
        self.created.append(unit_of_work)
        return unit_of_work


def test_setup_queries_return_detached_target_and_close_read_uow() -> None:
    repository = RecordingRepository()
    factory = RecordingFactory(repository)
    queries = MatchStaffSetupQueries(QueryRunner(factory))

    assert queries.search_targets(search=" 정기전 ", limit=25) == (_target(),)
    assert queries.get_target(match_id=71) == _target()

    assert repository.search_calls == [("정기전", 25)]
    assert repository.get_calls == [71]
    assert all(unit_of_work.rollbacks == 1 for unit_of_work in factory.created)
    assert all(unit_of_work.commits == 0 and unit_of_work.exited for unit_of_work in factory.created)


@pytest.mark.parametrize(
    ("method", "kwargs"),
    (("search", {"search": "x" * 201}), ("search", {"limit": 26}), ("get", {"match_id": 0})),
)
def test_setup_queries_reject_invalid_input_before_uow(method: str, kwargs: dict[str, object]) -> None:
    factory = RecordingFactory(RecordingRepository())
    queries = MatchStaffSetupQueries(QueryRunner(factory))

    with pytest.raises(ValueError):
        if method == "search":
            queries.search_targets(**kwargs)  # type: ignore[arg-type]
        else:
            queries.get_target(**kwargs)  # type: ignore[arg-type]

    assert factory.created == []


def test_setup_queries_map_malformed_repository_facts() -> None:
    queries = MatchStaffSetupQueries(QueryRunner(RecordingFactory(RecordingRepository(error=ValueError("bad")))))

    with pytest.raises(MatchStaffSetupInvalidSourceError):
        queries.get_target(match_id=71)
