"""Staff Match-creation course projection tests."""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType

import pytest

from uma_st2.application.execution import QueryRunner
from uma_st2.application.match import (
    MatchCourseChoice,
    MatchStaffCreationQueries,
    MatchStaffCreationResultOverflowError,
)
from uma_st2.domain.match import MatchDirection, MatchSurface, StadiumCourseLayout


def _choice(course_id: int) -> MatchCourseChoice:
    return MatchCourseChoice(
        id=course_id,
        stadium_id=1,
        stadium_name="도쿄",
        surface=MatchSurface.TURF,
        distance=2400,
        direction=MatchDirection.LEFT,
        layout=StadiumCourseLayout.STANDARD,
    )


class RecordingRepository:
    def __init__(self, choices: tuple[MatchCourseChoice, ...]) -> None:
        self.choices = choices
        self.limits: list[int] = []

    def list_course_choices(self, *, limit: int) -> tuple[MatchCourseChoice, ...]:
        self.limits.append(limit)
        return self.choices[:limit]


@dataclass
class RecordingUnitOfWork:
    match_staff_creation_queries: RecordingRepository

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


def _queries(repository: RecordingRepository) -> MatchStaffCreationQueries:
    return MatchStaffCreationQueries(QueryRunner(lambda: RecordingUnitOfWork(repository)))


def test_course_choices_return_as_closed_bounded_projection() -> None:
    repository = RecordingRepository((_choice(1), _choice(2)))

    result = _queries(repository).list_course_choices(limit=25)

    assert [choice.id for choice in result] == [1, 2]
    assert repository.limits == [26]


def test_course_choices_fail_closed_on_bounded_projection_overflow() -> None:
    repository = RecordingRepository(tuple(_choice(index) for index in range(1, 4)))

    with pytest.raises(MatchStaffCreationResultOverflowError):
        _queries(repository).list_course_choices(limit=2)
