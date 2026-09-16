"""Application policy tests for reviewed initial master-data seeding."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from uma_st2.application.execution import CommandRunner
from uma_st2.application.master_data import (
    MasterDataSeedCommands,
    MasterDataSeedConflictError,
    MasterDataSeedError,
    MasterDataSnapshot,
    SeedMasterData,
    StadiumCourseSeed,
    StadiumSeed,
    UmamusumeSeed,
    UmamusumeVariantSeed,
)
from uma_st2.domain.match import MatchDirection, MatchSurface, StadiumCourseLayout

NOW = datetime(2026, 9, 4, 5, 0, tzinfo=UTC)


def _umamusume(external_id: int = 101) -> UmamusumeSeed:
    return UmamusumeSeed(external_id=external_id, name_jp=f"Uma {external_id}", name_ko=f"우마 {external_id}")


def _stadium(external_id: int = 201) -> StadiumSeed:
    return StadiumSeed(external_id=external_id, name_jp=f"Stadium {external_id}", name_ko=f"경기장 {external_id}")


def _course(external_id: int = 301, *, stadium_external_id: int = 201) -> StadiumCourseSeed:
    return StadiumCourseSeed(
        external_id=external_id,
        stadium_external_id=stadium_external_id,
        surface=MatchSurface.TURF,
        distance=1600,
        direction=MatchDirection.LEFT,
        layout=StadiumCourseLayout.STANDARD,
    )


def _command(*, manifest_checksum: str = "a" * 64) -> SeedMasterData:
    return SeedMasterData(
        source_identifier="reviewed-master-2026-09-04",
        manifest_checksum=manifest_checksum,
        umamusumes=(_umamusume(),),
        umamusume_variants=(
            UmamusumeVariantSeed(
                external_id=102,
                umamusume_external_id=101,
                name_jp="Variant 102",
                name_ko=None,
                release_date=date(2026, 1, 2),
            ),
        ),
        stadiums=(_stadium(),),
        stadium_courses=(_course(),),
    )


class FakeMasterDataSeedRepository:
    def __init__(self, current: MasterDataSnapshot | None = None) -> None:
        self.current = current or MasterDataSnapshot((), (), (), ())
        self.create_calls = 0
        self.created_at: datetime | None = None

    def lock_current(self) -> MasterDataSnapshot:
        return self.current

    def create(self, *, command: SeedMasterData, created_at: datetime) -> None:
        self.create_calls += 1
        self.created_at = created_at
        self.current = command.snapshot


class FakeMasterDataSeedUnitOfWork:
    def __init__(self, repository: FakeMasterDataSeedRepository) -> None:
        self.master_data_seed = repository
        self.commits = 0

    def __enter__(self) -> FakeMasterDataSeedUnitOfWork:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None


def _commands(repository: FakeMasterDataSeedRepository) -> MasterDataSeedCommands:
    return MasterDataSeedCommands(
        CommandRunner(lambda: FakeMasterDataSeedUnitOfWork(repository)),
        clock=lambda: NOW,
    )


def test_empty_graph_is_created_once_and_complete_graph_exact_retries() -> None:
    repository = FakeMasterDataSeedRepository()
    commands = _commands(repository)

    created = commands.seed(_command())
    retried = commands.seed(_command())

    assert created.created is True
    assert retried.created is False
    assert created.master_data_checksum == retried.master_data_checksum
    assert created.umamusume_count == created.umamusume_variant_count == 1
    assert created.stadium_count == created.stadium_course_count == 1
    assert repository.create_calls == 1
    assert repository.created_at == NOW


def test_semantically_identical_manifest_bytes_are_an_exact_graph_retry() -> None:
    repository = FakeMasterDataSeedRepository()
    commands = _commands(repository)
    commands.seed(_command(manifest_checksum="a" * 64))

    receipt = commands.seed(_command(manifest_checksum="b" * 64))

    assert receipt.created is False
    assert receipt.manifest_checksum == "b" * 64
    assert receipt.master_data_checksum == repository.current.master_data_checksum


def test_partial_extra_or_changed_current_graph_fails_closed() -> None:
    partial = MasterDataSnapshot(
        umamusumes=(_umamusume(),),
        umamusume_variants=(),
        stadiums=(),
        stadium_courses=(),
    )
    repository = FakeMasterDataSeedRepository(partial)

    with pytest.raises(MasterDataSeedConflictError, match="partial or differs"):
        _commands(repository).seed(_command())

    assert repository.create_calls == 0


def test_graph_order_is_canonical_for_checksum_and_comparison() -> None:
    first = SeedMasterData(
        source_identifier="reviewed-source",
        manifest_checksum="c" * 64,
        umamusumes=(_umamusume(102), _umamusume(101)),
        umamusume_variants=(),
        stadiums=(_stadium(202), _stadium(201)),
        stadium_courses=(_course(302, stadium_external_id=202), _course(301)),
    )
    second = SeedMasterData(
        source_identifier="reviewed-source",
        manifest_checksum="d" * 64,
        umamusumes=tuple(reversed(first.umamusumes)),
        umamusume_variants=(),
        stadiums=tuple(reversed(first.stadiums)),
        stadium_courses=tuple(reversed(first.stadium_courses)),
    )

    assert first.snapshot == second.snapshot
    assert first.master_data_checksum == second.master_data_checksum
    assert tuple(item.external_id for item in first.umamusumes) == (101, 102)


def test_seed_validates_parent_relationships_and_duplicate_course_tuple() -> None:
    with pytest.raises(MasterDataSeedError, match="reference a seeded Umamusume"):
        SeedMasterData(
            source_identifier="reviewed-source",
            manifest_checksum="a" * 64,
            umamusumes=(_umamusume(),),
            umamusume_variants=(UmamusumeVariantSeed(102, 999, "Variant", None, None),),
            stadiums=(_stadium(),),
            stadium_courses=(_course(),),
        )

    with pytest.raises(MasterDataSeedError, match="duplicate canonical course tuples"):
        SeedMasterData(
            source_identifier="reviewed-source",
            manifest_checksum="a" * 64,
            umamusumes=(_umamusume(),),
            umamusume_variants=(),
            stadiums=(_stadium(),),
            stadium_courses=(_course(301), _course(302)),
        )


@pytest.mark.parametrize("invalid_external_id", [True, 0, -1, 9_223_372_036_854_775_808])
def test_seed_rejects_invalid_external_ids(invalid_external_id: object) -> None:
    with pytest.raises(MasterDataSeedError, match="positive signed BIGINT"):
        UmamusumeSeed(invalid_external_id, "Uma", None)  # type: ignore[arg-type]


def test_seed_rejects_noncanonical_surrounding_whitespace() -> None:
    with pytest.raises(MasterDataSeedError, match="printable characters"):
        UmamusumeSeed(101, " Uma ", None)
