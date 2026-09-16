"""Read-only Match cancellation query boundary tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.execution import QueryRunner
from uma_st2.application.match import (
    MatchCancellationPreviewTarget,
    MatchCancellationTargetChoice,
    MatchStaffCancellationQueries,
    MatchStaffCancellationUnavailableError,
)
from uma_st2.domain.match import MatchGrade, MatchSourceKind, MatchStatus

SCHEDULED_AT = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _target(
    *,
    status: MatchStatus = MatchStatus.BETTING_CLOSED,
    source_kind: MatchSourceKind = MatchSourceKind.NATIVE_V2,
) -> MatchCancellationPreviewTarget:
    return MatchCancellationPreviewTarget(
        match_id=71,
        match_name="제12회 정기전",
        source_kind=source_kind,
        status=status,
        terminal_reason=None,
        grade=MatchGrade.G1,
        scheduled_at=SCHEDULED_AT,
        entry_count=3,
        active_bet_count=2,
        active_stake_total=40,
        affected_persona_count=1,
    )


class RecordingRepository:
    def __init__(self, target: MatchCancellationPreviewTarget | None = None) -> None:
        self.target = target
        self.calls: list[tuple[object, ...]] = []

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchCancellationTargetChoice, ...]:
        self.calls.append(("search", search, limit))
        return (
            MatchCancellationTargetChoice(
                match_id=71,
                match_name="제12회 정기전",
                status=MatchStatus.BETTING_CLOSED,
                active_bet_count=2,
            ),
        )

    def get_target(self, *, match_id: int) -> MatchCancellationPreviewTarget | None:
        self.calls.append(("get", match_id))
        return self.target


@dataclass
class RecordingUnitOfWork:
    match_staff_cancellation_queries: RecordingRepository
    commits: int = 0
    rollbacks: int = 0

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


def test_search_and_target_use_read_only_uows() -> None:
    repository = RecordingRepository(_target())
    factory = RecordingFactory(repository)
    queries = MatchStaffCancellationQueries(QueryRunner(factory))

    choices = queries.search_targets(search=" 정기전 ", limit=25)
    target = queries.get_target(match_id=71)

    assert choices[0].status == MatchStatus.BETTING_CLOSED
    assert target.active_stake_total == 40
    assert repository.calls == [("search", "정기전", 25), ("get", 71)]
    assert [unit_of_work.rollbacks for unit_of_work in factory.created] == [1, 1]
    assert [unit_of_work.commits for unit_of_work in factory.created] == [0, 0]


@pytest.mark.parametrize(
    "target",
    (
        _target(status=MatchStatus.SETTLED),
        _target(source_kind=MatchSourceKind.IMPORTED_V1),
    ),
)
def test_ineligible_target_is_rejected_without_write(target: MatchCancellationPreviewTarget) -> None:
    repository = RecordingRepository(target)
    factory = RecordingFactory(repository)
    queries = MatchStaffCancellationQueries(QueryRunner(factory))

    with pytest.raises(MatchStaffCancellationUnavailableError):
        queries.get_target(match_id=71)

    assert factory.created[0].rollbacks == 0
    assert factory.created[0].commits == 0
