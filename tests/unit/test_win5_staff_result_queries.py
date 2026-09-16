"""Staff Normal result query application and SQLAlchemy slice tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest
from sqlalchemy import create_engine

from uma_st2.application.execution import QueryRunner
from uma_st2.application.win5 import (
    Win5NormalResultEntryOption,
    Win5NormalResultTargetChoice,
    Win5NormalResultTargetMode,
    Win5NormalResultTargetSource,
    Win5SpecialResultRace,
    Win5SpecialResultReferenceEntry,
    Win5SpecialResultTargetChoice,
    Win5SpecialResultTargetMode,
    Win5SpecialResultTargetSource,
    Win5StaffResultInvalidSourceError,
    Win5StaffResultQueries,
    Win5StaffResultTargetUnavailableError,
)
from uma_st2.compose import compose_win5_staff_result_queries
from uma_st2.domain.win5 import (
    Win5NormalResultPlacement,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SpecialResultWinner,
    fingerprint_normal_result,
    fingerprint_special_result,
)
from uma_st2.infrastructure.database import Base, DatabaseRuntime
from uma_st2.infrastructure.database.orm import (
    Win5RaceEntryORM,
    Win5RaceORM,
    Win5ResultORM,
    Win5RoundORM,
    Win5SeasonORM,
)

NOW = datetime(2026, 8, 25, 0, 0)


def _entries(*, count: int = 6) -> tuple[Win5NormalResultEntryOption, ...]:
    return tuple(
        Win5NormalResultEntryOption(
            id=100 + gate_number,
            gate_number=gate_number,
            name=f"entry-{gate_number}",
        )
        for gate_number in range(1, count + 1)
    )


def _placements(*, count: int = 5) -> tuple[Win5NormalResultPlacement, ...]:
    return tuple(
        Win5NormalResultPlacement(
            id=200 + position,
            position=position,
            race_entry_id=100 + position,
        )
        for position in range(1, count + 1)
    )


def _source(
    *,
    placements: tuple[Win5NormalResultPlacement, ...] = (),
    has_score_events: bool = False,
    round_status: Win5RoundStatus = Win5RoundStatus.CLOSED,
) -> Win5NormalResultTargetSource:
    return Win5NormalResultTargetSource(
        season_id=7,
        season_name="2026 하반기",
        season_status=Win5SeasonStatus.ACTIVE,
        round_id=11,
        round_name="제3회 아리마 기념",
        round_type=Win5RoundType.NORMAL,
        round_status=round_status,
        race_id=17,
        race_name="아리마 기념",
        entries=_entries(),
        placements=placements,
        has_score_events=has_score_events,
    )


def _special_races(*, void_ids: tuple[int, ...] = ()) -> tuple[Win5SpecialResultRace, ...]:
    return tuple(
        Win5SpecialResultRace(
            id=race_id,
            name=f"Race {race_id}",
            entries=(
                Win5SpecialResultReferenceEntry(
                    id=(race_id * 10) + 1,
                    gate_number=1,
                    name=f"reference-{race_id}",
                ),
            ),
            void_reason=f"void {race_id}" if race_id in void_ids else None,
            voided_at=datetime(2026, 8, 26, tzinfo=UTC) if race_id in void_ids else None,
        )
        for race_id in (101, 102, 103)
    )


def _special_winners(*, count: int = 3) -> tuple[Win5SpecialResultWinner, ...]:
    return tuple(
        Win5SpecialResultWinner(
            id=200 + index,
            race_id=race_id,
            gate_number=gate_number,
        )
        for index, (race_id, gate_number) in enumerate(
            ((101, 9), (102, 7), (103, 3)),
            start=1,
        )
    )[:count]


def _special_source(
    *,
    winners: tuple[Win5SpecialResultWinner, ...] = (),
    has_score_events: bool = False,
    void_ids: tuple[int, ...] = (),
) -> Win5SpecialResultTargetSource:
    return Win5SpecialResultTargetSource(
        season_id=7,
        season_name="2026 하반기",
        season_status=Win5SeasonStatus.ACTIVE,
        round_id=21,
        round_name="여름 Special",
        round_type=Win5RoundType.SPECIAL,
        round_status=Win5RoundStatus.CLOSED,
        races=_special_races(void_ids=void_ids),
        winners=winners,
        has_score_events=has_score_events,
    )


class RecordingRepository:
    def __init__(
        self,
        *,
        choices: tuple[Win5NormalResultTargetChoice, ...] = (),
        source: Win5NormalResultTargetSource | None = None,
        special_choices: tuple[Win5SpecialResultTargetChoice, ...] = (),
        special_source: Win5SpecialResultTargetSource | None = None,
    ) -> None:
        self.choices = choices
        self.source = source
        self.special_choices = special_choices
        self.special_source = special_source
        self.list_calls: list[tuple[Win5NormalResultTargetMode, int]] = []
        self.get_calls: list[int] = []
        self.special_list_calls: list[tuple[Win5SpecialResultTargetMode, int]] = []
        self.special_get_calls: list[int] = []

    def list_normal_result_target_choices(
        self,
        *,
        mode: Win5NormalResultTargetMode,
        limit: int,
    ) -> tuple[Win5NormalResultTargetChoice, ...]:
        self.list_calls.append((mode, limit))
        return self.choices

    def get_normal_result_target_source(
        self,
        *,
        round_id: int,
    ) -> Win5NormalResultTargetSource | None:
        self.get_calls.append(round_id)
        return self.source

    def list_special_result_target_choices(
        self,
        *,
        mode: Win5SpecialResultTargetMode,
        limit: int,
    ) -> tuple[Win5SpecialResultTargetChoice, ...]:
        self.special_list_calls.append((mode, limit))
        return self.special_choices

    def get_special_result_target_source(
        self,
        *,
        round_id: int,
    ) -> Win5SpecialResultTargetSource | None:
        self.special_get_calls.append(round_id)
        return self.special_source


@dataclass
class RecordingUnitOfWork:
    win5_staff_result_queries: RecordingRepository
    commit_count: int = 0
    rollback_count: int = 0

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


def test_staff_result_list_and_editor_use_query_runner_without_commit() -> None:
    choice = Win5NormalResultTargetChoice(
        season_id=7,
        season_name="2026 하반기",
        round_id=11,
        round_name="제3회 아리마 기념",
        race_id=17,
        race_name="아리마 기념",
        mode=Win5NormalResultTargetMode.CORRECTION,
    )
    source = _source(placements=_placements())
    repository = RecordingRepository(choices=(choice,), source=source)
    factory = RecordingFactory(repository)
    queries = Win5StaffResultQueries(QueryRunner(factory))

    assert queries.list_normal_result_targets(mode=Win5NormalResultTargetMode.CORRECTION) == (choice,)
    target = queries.get_normal_result_target(
        round_id=11,
        expected_mode=Win5NormalResultTargetMode.CORRECTION,
    )

    assert target.choice == choice
    assert target.result_fingerprint == fingerprint_normal_result(_placements())
    assert repository.list_calls == [(Win5NormalResultTargetMode.CORRECTION, 25)]
    assert repository.get_calls == [11]
    assert all(unit_of_work.commit_count == 0 for unit_of_work in factory.created)
    assert all(unit_of_work.rollback_count == 1 for unit_of_work in factory.created)


def test_staff_result_editor_rejects_partial_score_backed_or_changed_mode() -> None:
    partial = RecordingRepository(source=_source(placements=_placements(count=4)))
    with pytest.raises(Win5StaffResultInvalidSourceError, match="complete canonical"):
        Win5StaffResultQueries(QueryRunner(RecordingFactory(partial))).get_normal_result_target(
            round_id=11,
            expected_mode=Win5NormalResultTargetMode.CORRECTION,
        )

    score_backed = RecordingRepository(source=_source(placements=_placements(), has_score_events=True))
    with pytest.raises(Win5StaffResultTargetUnavailableError, match="immutable"):
        Win5StaffResultQueries(QueryRunner(RecordingFactory(score_backed))).get_normal_result_target(
            round_id=11,
            expected_mode=Win5NormalResultTargetMode.CORRECTION,
        )

    changed_mode = RecordingRepository(source=_source(placements=_placements()))
    with pytest.raises(Win5StaffResultTargetUnavailableError, match="changed state"):
        Win5StaffResultQueries(QueryRunner(RecordingFactory(changed_mode))).get_normal_result_target(
            round_id=11,
            expected_mode=Win5NormalResultTargetMode.ENTRY,
        )


def test_special_result_list_and_editor_keep_reference_entries_non_authoritative() -> None:
    choice = Win5SpecialResultTargetChoice(
        season_id=7,
        season_name="2026 하반기",
        round_id=21,
        round_name="여름 Special",
        race_count=3,
        mode=Win5SpecialResultTargetMode.CORRECTION,
    )
    winners = _special_winners()
    repository = RecordingRepository(
        special_choices=(choice,),
        special_source=_special_source(winners=winners),
    )
    factory = RecordingFactory(repository)
    queries = Win5StaffResultQueries(QueryRunner(factory))

    assert queries.list_special_result_targets(mode=Win5SpecialResultTargetMode.CORRECTION) == (choice,)
    target = queries.get_special_result_target(
        round_id=21,
        expected_mode=Win5SpecialResultTargetMode.CORRECTION,
    )

    assert target.choice == choice
    assert target.result_fingerprint == fingerprint_special_result(winners)
    assert tuple(winner.gate_number for winner in target.current_winners) == (9, 7, 3)
    assert tuple(race.entries[0].gate_number for race in target.races) == (1, 1, 1)
    assert repository.special_list_calls == [(Win5SpecialResultTargetMode.CORRECTION, 25)]
    assert repository.special_get_calls == [21]
    assert all(unit_of_work.commit_count == 0 for unit_of_work in factory.created)


def test_special_result_editor_accepts_mixed_void_and_rejects_partial_score_backed_or_changed_mode() -> None:
    mixed_winners = (
        Win5SpecialResultWinner(id=201, race_id=101, gate_number=9),
        Win5SpecialResultWinner(id=203, race_id=103, gate_number=3),
    )
    mixed = RecordingRepository(
        special_source=_special_source(
            winners=mixed_winners,
            void_ids=(102,),
        )
    )
    mixed_target = Win5StaffResultQueries(QueryRunner(RecordingFactory(mixed))).get_special_result_target(
        round_id=21,
        expected_mode=Win5SpecialResultTargetMode.CORRECTION,
    )

    assert tuple(race.id for race in mixed_target.non_void_races) == (101, 103)
    assert mixed_target.races[1].is_void
    assert mixed_target.choice.void_count == 1
    assert mixed_target.result_fingerprint == fingerprint_special_result(mixed_winners)

    partial = RecordingRepository(special_source=_special_source(winners=_special_winners(count=2)))
    with pytest.raises(Win5StaffResultInvalidSourceError, match="non-void Race"):
        Win5StaffResultQueries(QueryRunner(RecordingFactory(partial))).get_special_result_target(
            round_id=21,
            expected_mode=Win5SpecialResultTargetMode.CORRECTION,
        )

    result_on_void = RecordingRepository(
        special_source=_special_source(
            winners=_special_winners(),
            void_ids=(102,),
        )
    )
    with pytest.raises(Win5StaffResultInvalidSourceError, match="non-void Race"):
        Win5StaffResultQueries(QueryRunner(RecordingFactory(result_on_void))).get_special_result_target(
            round_id=21,
            expected_mode=Win5SpecialResultTargetMode.CORRECTION,
        )

    score_backed = RecordingRepository(
        special_source=_special_source(
            winners=_special_winners(),
            has_score_events=True,
        )
    )
    with pytest.raises(Win5StaffResultTargetUnavailableError, match="immutable"):
        Win5StaffResultQueries(QueryRunner(RecordingFactory(score_backed))).get_special_result_target(
            round_id=21,
            expected_mode=Win5SpecialResultTargetMode.CORRECTION,
        )

    changed_mode = RecordingRepository(special_source=_special_source(winners=_special_winners()))
    with pytest.raises(Win5StaffResultTargetUnavailableError, match="changed state"):
        Win5StaffResultQueries(QueryRunner(RecordingFactory(changed_mode))).get_special_result_target(
            round_id=21,
            expected_mode=Win5SpecialResultTargetMode.ENTRY,
        )


@pytest.mark.parametrize(
    ("method", "kwargs", "message"),
    [
        ("list", {"mode": "unknown"}, "entry.*correction"),
        ("list", {"mode": "entry", "limit": 26}, "1 through 25"),
        ("get", {"round_id": True, "expected_mode": "entry"}, "positive integer"),
        ("get", {"round_id": 1, "expected_mode": "unknown"}, "entry.*correction"),
    ],
)
def test_staff_result_queries_reject_invalid_inputs_without_opening_uow(
    method: str,
    kwargs: dict[str, object],
    message: str,
) -> None:
    factory = RecordingFactory(RecordingRepository())
    queries = Win5StaffResultQueries(QueryRunner(factory))

    with pytest.raises(ValueError, match=message):
        if method == "list":
            queries.list_normal_result_targets(**kwargs)  # type: ignore[arg-type]
        else:
            queries.get_normal_result_target(**kwargs)  # type: ignore[arg-type]

    assert factory.created == []


def _round_row(id_: int, *, status: str = "closed") -> dict[str, object]:
    return {
        "id": id_,
        "season_id": 1,
        "type": "normal",
        "status": status,
        "name": f"Round {id_}",
        "created_at": NOW,
        "updated_at": NOW,
    }


def test_composed_staff_result_queries_filter_targets_and_build_current_fingerprint() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            connection.execute(
                Win5SeasonORM.__table__.insert().values(
                    id=1,
                    name="2026 하반기",
                    status="active",
                    active_marker=True,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            connection.execute(
                Win5RoundORM.__table__.insert(),
                [
                    _round_row(10),
                    _round_row(11),
                    _round_row(12),
                    _round_row(13, status="open"),
                ],
            )
            connection.execute(
                Win5RaceORM.__table__.insert(),
                [
                    {
                        "id": round_id * 10,
                        "round_id": round_id,
                        "name": f"Race {round_id}",
                        "created_at": NOW,
                        "updated_at": NOW,
                    }
                    for round_id in (10, 11, 12, 13)
                ],
            )
            connection.execute(
                Win5RaceEntryORM.__table__.insert(),
                [
                    {
                        "id": (round_id * 100) + gate_number,
                        "race_id": round_id * 10,
                        "gate_number": gate_number,
                        "name": f"Horse {round_id}-{gate_number}",
                        "created_at": NOW,
                        "updated_at": NOW,
                    }
                    for round_id in (10, 11, 12, 13)
                    for gate_number in range(1, 7)
                ],
            )
            connection.execute(
                Win5ResultORM.__table__.insert(),
                [
                    {
                        "id": (round_id * 1000) + position,
                        "race_id": round_id * 10,
                        "race_entry_id": (round_id * 100) + position,
                        "gate_number": None,
                        "position": position,
                        "created_at": NOW,
                    }
                    for round_id, count in ((11, 5), (12, 4))
                    for position in range(1, count + 1)
                ],
            )

        queries = compose_win5_staff_result_queries(runtime)
        entry_choices = queries.list_normal_result_targets(mode=Win5NormalResultTargetMode.ENTRY)
        correction_choices = queries.list_normal_result_targets(mode=Win5NormalResultTargetMode.CORRECTION)
        target = queries.get_normal_result_target(
            round_id=11,
            expected_mode=Win5NormalResultTargetMode.CORRECTION,
        )

        assert tuple(choice.round_id for choice in entry_choices) == (10,)
        assert tuple(choice.round_id for choice in correction_choices) == (11,)
        assert tuple(entry.gate_number for entry in target.entries) == (1, 2, 3, 4, 5, 6)
        assert target.result_fingerprint == fingerprint_normal_result(target.current_placements)
        with pytest.raises(Win5StaffResultInvalidSourceError):
            queries.get_normal_result_target(
                round_id=12,
                expected_mode=Win5NormalResultTargetMode.CORRECTION,
            )
        with pytest.raises(Win5StaffResultTargetUnavailableError):
            queries.get_normal_result_target(
                round_id=13,
                expected_mode=Win5NormalResultTargetMode.ENTRY,
            )
    finally:
        runtime.dispose()


def test_composed_staff_result_queries_build_special_bundle_without_reference_gate_constraint() -> None:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    try:
        with runtime.engine.begin() as connection:
            connection.execute(
                Win5SeasonORM.__table__.insert().values(
                    id=1,
                    name="2026 하반기",
                    status="active",
                    active_marker=True,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            connection.execute(
                Win5RoundORM.__table__.insert(),
                [
                    {
                        "id": round_id,
                        "season_id": 1,
                        "type": "special",
                        "status": status,
                        "name": f"Special {round_id}",
                        "created_at": NOW,
                        "updated_at": NOW,
                    }
                    for round_id, status in (
                        (20, "closed"),
                        (21, "closed"),
                        (22, "closed"),
                        (23, "open"),
                        (24, "closed"),
                    )
                ],
            )
            connection.execute(
                Win5RaceORM.__table__.insert(),
                [
                    {
                        "id": (round_id * 10) + race_index,
                        "round_id": round_id,
                        "name": f"Race {round_id}-{race_index}",
                        "void_reason": "official void" if round_id == 24 and race_index == 1 else None,
                        "voided_at": NOW if round_id == 24 and race_index == 1 else None,
                        "created_at": NOW,
                        "updated_at": NOW,
                    }
                    for round_id in (20, 21, 22, 23, 24)
                    for race_index in range(1, 4)
                ],
            )
            connection.execute(
                Win5RaceEntryORM.__table__.insert(),
                [
                    {
                        "id": (round_id * 100) + race_index,
                        "race_id": (round_id * 10) + race_index,
                        "gate_number": 1,
                        "name": f"Reference {round_id}-{race_index}",
                        "created_at": NOW,
                        "updated_at": NOW,
                    }
                    for round_id in (20, 21, 22, 23, 24)
                    for race_index in range(1, 4)
                ],
            )
            connection.execute(
                Win5ResultORM.__table__.insert(),
                [
                    {
                        "id": (round_id * 1000) + race_index,
                        "race_id": (round_id * 10) + race_index,
                        "race_entry_id": None,
                        "gate_number": 8 + race_index,
                        "position": 1,
                        "created_at": NOW,
                    }
                    for round_id, count in ((21, 3), (22, 2))
                    for race_index in range(1, count + 1)
                ],
            )

        queries = compose_win5_staff_result_queries(runtime)
        entry_choices = queries.list_special_result_targets(mode=Win5SpecialResultTargetMode.ENTRY)
        correction_choices = queries.list_special_result_targets(mode=Win5SpecialResultTargetMode.CORRECTION)
        target = queries.get_special_result_target(
            round_id=21,
            expected_mode=Win5SpecialResultTargetMode.CORRECTION,
        )

        assert tuple(choice.round_id for choice in entry_choices) == (20, 24)
        assert tuple(choice.void_count for choice in entry_choices) == (0, 1)
        assert tuple(choice.round_id for choice in correction_choices) == (21,)
        assert tuple(winner.gate_number for winner in target.current_winners) == (9, 10, 11)
        assert tuple(race.entries[0].gate_number for race in target.races) == (1, 1, 1)
        assert target.result_fingerprint == fingerprint_special_result(target.current_winners)
        with pytest.raises(Win5StaffResultInvalidSourceError):
            queries.get_special_result_target(
                round_id=22,
                expected_mode=Win5SpecialResultTargetMode.CORRECTION,
            )
        with pytest.raises(Win5StaffResultTargetUnavailableError):
            queries.get_special_result_target(
                round_id=23,
                expected_mode=Win5SpecialResultTargetMode.ENTRY,
            )
        mixed_target = queries.get_special_result_target(
            round_id=24,
            expected_mode=Win5SpecialResultTargetMode.ENTRY,
        )
        assert mixed_target.races[0].is_void
        assert tuple(race.id for race in mixed_target.non_void_races) == (242, 243)
    finally:
        runtime.dispose()
