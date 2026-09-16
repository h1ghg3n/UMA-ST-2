"""Application query boundary tests for native member Match betting reads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.betting import (
    MatchBetInputPointState,
    MatchBetTargetChoice,
    MatchMemberBettingInvalidSourceError,
    MatchMemberBettingQueries,
    MatchRaceDetail,
    MatchRaceDetailCondition,
    MatchRaceDetailCourse,
    MatchRaceDetailEntry,
    MatchRaceDetailUnavailableError,
    MatchRaceListItem,
    MemberMatchBetHistoryItem,
)
from uma_st2.application.execution import QueryRunner
from uma_st2.domain.betting import BetStatus, BetType
from uma_st2.domain.match import (
    MatchDirection,
    MatchGrade,
    MatchSeason,
    MatchStatus,
    MatchSurface,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
    StadiumCourseLayout,
)


def _target() -> MatchBetTargetChoice:
    return MatchBetTargetChoice(
        match_id=71,
        match_name="제12회 정기전",
        grade=MatchGrade.G1,
        scheduled_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        entry_count=3,
    )


def _race() -> MatchRaceListItem:
    return MatchRaceListItem(
        match_id=70,
        match_name="예약 경기",
        grade=MatchGrade.OP,
        scheduled_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        status=MatchStatus.SCHEDULED,
        entry_count=0,
    )


def _history_item() -> MemberMatchBetHistoryItem:
    return MemberMatchBetHistoryItem(
        bet_id=801,
        match_name="제12회 정기전",
        scheduled_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        match_status=MatchStatus.BETTING_CLOSED,
        bet_type=BetType.QUINELLA,
        entry_numbers=(1, 3),
        amount=30,
        bet_status=BetStatus.ACTIVE,
        created_at=datetime(2026, 8, 29, 10, 0, tzinfo=UTC),
    )


def _detail() -> MatchRaceDetail:
    return MatchRaceDetail(
        match_id=70,
        match_name="예약 경기",
        description="설명",
        grade=MatchGrade.OP,
        scheduled_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        status=MatchStatus.SCHEDULED,
        course=MatchRaceDetailCourse(
            stadium_name="도쿄",
            surface=MatchSurface.TURF,
            distance=2400,
            direction=MatchDirection.LEFT,
            layout=StadiumCourseLayout.STANDARD,
        ),
        condition=MatchRaceDetailCondition(
            season=MatchSeason.SPRING,
            weather=MatchWeather.SUNNY,
            time_of_day=MatchTimeOfDay.DAY,
            track_condition=MatchTrackCondition.FIRM,
        ),
        entries=(
            MatchRaceDetailEntry(
                entry_number=1,
                game_account_name="주자 1",
                umamusume_name="스페셜 위크",
                affiliation="서클",
            ),
        ),
    )


_DEFAULT_DETAIL = object()


class RecordingRepository:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        personal_error: Exception | None = None,
        balance: object = 500,
        balance_error: Exception | None = None,
        detail: MatchRaceDetail | None | object = _DEFAULT_DETAIL,
        detail_error: Exception | None = None,
    ) -> None:
        self.error = error
        self.personal_error = personal_error
        self.balance = balance
        self.balance_error = balance_error
        self.detail = _detail() if detail is _DEFAULT_DETAIL else detail
        self.detail_error = detail_error
        self.calls: list[tuple[str, int]] = []
        self.race_calls: list[int] = []
        self.race_search_calls: list[tuple[str, int]] = []
        self.race_detail_calls: list[int] = []
        self.personal_calls: list[tuple[str, int]] = []
        self.balance_calls: list[str] = []

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchBetTargetChoice, ...]:
        self.calls.append((search, limit))
        if self.error is not None:
            raise self.error
        return (_target(),)

    def list_races(self, *, limit: int) -> tuple[MatchRaceListItem, ...]:
        self.race_calls.append(limit)
        if self.error is not None:
            raise self.error
        return (_race(), _target_race())

    def search_races(self, *, search: str, limit: int) -> tuple[MatchRaceListItem, ...]:
        self.race_search_calls.append((search, limit))
        if self.error is not None:
            raise self.error
        return (_race(), _target_race())

    def get_race_detail(self, *, match_id: int) -> MatchRaceDetail | None:
        self.race_detail_calls.append(match_id)
        if self.detail_error is not None:
            raise self.detail_error
        return self.detail  # type: ignore[return-value]

    def get_current_balance(self, *, actor_discord_user_id: str) -> int | None:
        self.balance_calls.append(actor_discord_user_id)
        if self.balance_error is not None:
            raise self.balance_error
        return self.balance  # type: ignore[return-value]

    def search_active_bets(self, **kwargs: object) -> tuple[()]:
        raise AssertionError("Open Match listing must not query actor-owned Bets.")

    def list_personal_bets(
        self,
        *,
        actor_discord_user_id: str,
        limit: int,
    ) -> tuple[MemberMatchBetHistoryItem, ...]:
        self.personal_calls.append((actor_discord_user_id, limit))
        if self.personal_error is not None:
            raise self.personal_error
        return (_history_item(),)


@dataclass
class RecordingUnitOfWork:
    match_member_betting_queries: RecordingRepository
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


def _target_race() -> MatchRaceListItem:
    target = _target()
    return MatchRaceListItem(
        match_id=target.match_id,
        match_name=target.match_name,
        grade=target.grade,
        scheduled_at=target.scheduled_at,
        status=MatchStatus.BETTING_OPEN,
        entry_count=target.entry_count,
    )


def test_race_list_uses_bounded_query_runner_without_commit() -> None:
    repository = RecordingRepository()
    factory = RecordingFactory(repository)
    queries = MatchMemberBettingQueries(QueryRunner(factory))

    assert queries.list_races(limit=10) == (_race(), _target_race())

    assert repository.race_calls == [10]
    assert repository.calls == []
    assert len(factory.created) == 1
    unit_of_work = factory.created[0]
    assert unit_of_work.entered and unit_of_work.exited
    assert unit_of_work.commit_count == 0
    assert unit_of_work.rollback_count == 1


@pytest.mark.parametrize("limit", (0, 11, True))
def test_race_list_rejects_invalid_limit_before_opening_uow(limit: object) -> None:
    factory = RecordingFactory(RecordingRepository())
    queries = MatchMemberBettingQueries(QueryRunner(factory))

    with pytest.raises(ValueError, match="between 1 and 10"):
        queries.list_races(limit=limit)  # type: ignore[arg-type]

    assert factory.created == []


def test_race_list_maps_malformed_projection_to_application_error() -> None:
    repository = RecordingRepository(error=ValueError("invalid grade"))
    queries = MatchMemberBettingQueries(QueryRunner(RecordingFactory(repository)))

    with pytest.raises(MatchMemberBettingInvalidSourceError):
        queries.list_races()


def test_race_search_normalizes_input_and_uses_read_only_query_runner() -> None:
    repository = RecordingRepository()
    factory = RecordingFactory(repository)
    queries = MatchMemberBettingQueries(QueryRunner(factory))

    assert queries.search_races(search="  예약  ", limit=25) == (_race(), _target_race())

    assert repository.race_search_calls == [("예약", 25)]
    unit_of_work = factory.created[0]
    assert unit_of_work.entered and unit_of_work.exited
    assert unit_of_work.commit_count == 0
    assert unit_of_work.rollback_count == 1


@pytest.mark.parametrize(
    ("search", "limit"),
    ((None, 25), ("x" * 201, 25), ("", 0), ("", 26), ("", True)),
)
def test_race_search_rejects_invalid_input_before_opening_uow(search: object, limit: object) -> None:
    factory = RecordingFactory(RecordingRepository())
    queries = MatchMemberBettingQueries(QueryRunner(factory))

    with pytest.raises(ValueError):
        queries.search_races(search=search, limit=limit)  # type: ignore[arg-type]

    assert factory.created == []


def test_race_detail_uses_fresh_read_only_query_and_returns_complete_projection() -> None:
    repository = RecordingRepository()
    factory = RecordingFactory(repository)
    queries = MatchMemberBettingQueries(QueryRunner(factory))

    assert queries.get_race_detail(match_id=70) == _detail()

    assert repository.race_detail_calls == [70]
    unit_of_work = factory.created[0]
    assert unit_of_work.entered and unit_of_work.exited
    assert unit_of_work.commit_count == 0
    assert unit_of_work.rollback_count == 1


@pytest.mark.parametrize("match_id", (0, -1, True))
def test_race_detail_rejects_invalid_id_before_opening_uow(match_id: object) -> None:
    factory = RecordingFactory(RecordingRepository())
    queries = MatchMemberBettingQueries(QueryRunner(factory))

    with pytest.raises(ValueError, match="positive integer"):
        queries.get_race_detail(match_id=match_id)  # type: ignore[arg-type]

    assert factory.created == []


def test_race_detail_maps_absent_or_stale_target_to_expected_unavailable() -> None:
    repository = RecordingRepository(detail=None)
    queries = MatchMemberBettingQueries(QueryRunner(RecordingFactory(repository)))

    with pytest.raises(MatchRaceDetailUnavailableError):
        queries.get_race_detail(match_id=70)


def test_race_detail_maps_malformed_projection_to_application_error() -> None:
    repository = RecordingRepository(detail_error=ValueError("missing condition"))
    queries = MatchMemberBettingQueries(QueryRunner(RecordingFactory(repository)))

    with pytest.raises(MatchMemberBettingInvalidSourceError):
        queries.get_race_detail(match_id=70)


def test_personal_bet_list_uses_actor_scoped_query_runner_without_commit() -> None:
    repository = RecordingRepository()
    factory = RecordingFactory(repository)
    queries = MatchMemberBettingQueries(QueryRunner(factory))

    assert queries.list_personal_bets(actor_discord_user_id=" 123456789 ", limit=10) == (_history_item(),)

    assert repository.personal_calls == [("123456789", 10)]
    assert len(factory.created) == 1
    unit_of_work = factory.created[0]
    assert unit_of_work.entered and unit_of_work.exited
    assert unit_of_work.commit_count == 0
    assert unit_of_work.rollback_count == 1


def test_bet_input_point_state_uses_fresh_actor_scoped_query_without_commit() -> None:
    repository = RecordingRepository(balance=880)
    factory = RecordingFactory(repository)
    queries = MatchMemberBettingQueries(QueryRunner(factory))

    assert queries.get_input_point_state(actor_discord_user_id=" 123456789 ") == MatchBetInputPointState(
        balance=880,
        maximum_stake=80,
    )

    assert repository.balance_calls == ["123456789"]
    unit_of_work = factory.created[0]
    assert unit_of_work.entered and unit_of_work.exited
    assert unit_of_work.commit_count == 0
    assert unit_of_work.rollback_count == 1


def test_bet_input_point_state_returns_none_without_linked_wallet() -> None:
    queries = MatchMemberBettingQueries(QueryRunner(RecordingFactory(RecordingRepository(balance=None))))

    assert queries.get_input_point_state(actor_discord_user_id="123") is None


def test_bet_input_point_state_maps_malformed_balance_to_application_error() -> None:
    queries = MatchMemberBettingQueries(QueryRunner(RecordingFactory(RecordingRepository(balance=True))))

    with pytest.raises(MatchMemberBettingInvalidSourceError):
        queries.get_input_point_state(actor_discord_user_id="123")


@pytest.mark.parametrize(
    ("actor_discord_user_id", "limit"),
    (("", 10), ("1" * 33, 10), ("123", 0), ("123", 11), ("123", True)),
)
def test_personal_bet_list_rejects_invalid_input_before_opening_uow(
    actor_discord_user_id: str,
    limit: object,
) -> None:
    factory = RecordingFactory(RecordingRepository())
    queries = MatchMemberBettingQueries(QueryRunner(factory))

    with pytest.raises(ValueError):
        queries.list_personal_bets(
            actor_discord_user_id=actor_discord_user_id,
            limit=limit,  # type: ignore[arg-type]
        )

    assert factory.created == []


def test_personal_bet_list_maps_malformed_projection_to_application_error() -> None:
    repository = RecordingRepository(personal_error=ValueError("invalid selection"))
    queries = MatchMemberBettingQueries(QueryRunner(RecordingFactory(repository)))

    with pytest.raises(MatchMemberBettingInvalidSourceError):
        queries.list_personal_bets(actor_discord_user_id="123")
