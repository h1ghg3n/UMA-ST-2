"""Create-only reviewed master-data seed command."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from hashlib import sha256
from typing import Protocol

from uma_st2.application.execution import CommandRunner, UnitOfWork
from uma_st2.domain.match import MatchDirection, MatchSurface, StadiumCourseLayout

_MAX_BIGINT = 9_223_372_036_854_775_807


class MasterDataSeedError(ValueError):
    """A reviewed master-data seed request is malformed."""


class MasterDataSeedConflictError(MasterDataSeedError):
    """The current master graph is not the requested exact seed graph."""


def _positive_bigint(value: object, *, field_name: str) -> int:
    if type(value) is not int or value <= 0 or value > _MAX_BIGINT:
        raise MasterDataSeedError(f"{field_name} must be a positive signed BIGINT.")
    return value


def _positive_integer(value: object, *, field_name: str) -> int:
    if type(value) is not int or value <= 0 or value > 2_147_483_647:
        raise MasterDataSeedError(f"{field_name} must be a positive signed INTEGER.")
    return value


def _required_text(value: object, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise MasterDataSeedError(f"{field_name} must be text.")
    normalized = value.strip()
    if (
        not normalized
        or normalized != value
        or len(normalized) > max_length
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise MasterDataSeedError(f"{field_name} must contain 1 to {max_length} printable characters.")
    return normalized


def _optional_text(value: object, *, field_name: str, max_length: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name=field_name, max_length=max_length)


def _sha256_hex(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise MasterDataSeedError(f"{field_name} must be a SHA-256 hex string.")
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise MasterDataSeedError(f"{field_name} must be a SHA-256 hex string.")
    return normalized


@dataclass(frozen=True, slots=True)
class UmamusumeSeed:
    external_id: int
    name_jp: str
    name_ko: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "external_id", _positive_bigint(self.external_id, field_name="external_id"))
        object.__setattr__(self, "name_jp", _required_text(self.name_jp, field_name="name_jp", max_length=100))
        object.__setattr__(self, "name_ko", _optional_text(self.name_ko, field_name="name_ko", max_length=100))


@dataclass(frozen=True, slots=True)
class UmamusumeVariantSeed:
    external_id: int
    umamusume_external_id: int
    name_jp: str
    name_ko: str | None
    release_date: date | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "external_id", _positive_bigint(self.external_id, field_name="external_id"))
        object.__setattr__(
            self,
            "umamusume_external_id",
            _positive_bigint(self.umamusume_external_id, field_name="umamusume_external_id"),
        )
        object.__setattr__(self, "name_jp", _required_text(self.name_jp, field_name="name_jp", max_length=100))
        object.__setattr__(self, "name_ko", _optional_text(self.name_ko, field_name="name_ko", max_length=100))
        if self.release_date is not None and type(self.release_date) is not date:
            raise MasterDataSeedError("release_date must be a date or null.")


@dataclass(frozen=True, slots=True)
class StadiumSeed:
    external_id: int
    name_jp: str
    name_ko: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "external_id", _positive_bigint(self.external_id, field_name="external_id"))
        object.__setattr__(self, "name_jp", _required_text(self.name_jp, field_name="name_jp", max_length=100))
        object.__setattr__(self, "name_ko", _optional_text(self.name_ko, field_name="name_ko", max_length=100))


@dataclass(frozen=True, slots=True)
class StadiumCourseSeed:
    external_id: int
    stadium_external_id: int
    surface: MatchSurface
    distance: int
    direction: MatchDirection
    layout: StadiumCourseLayout

    def __post_init__(self) -> None:
        object.__setattr__(self, "external_id", _positive_bigint(self.external_id, field_name="external_id"))
        object.__setattr__(
            self,
            "stadium_external_id",
            _positive_bigint(self.stadium_external_id, field_name="stadium_external_id"),
        )
        object.__setattr__(self, "distance", _positive_integer(self.distance, field_name="distance"))
        if not isinstance(self.surface, MatchSurface):
            raise MasterDataSeedError("surface must be a canonical MatchSurface.")
        if not isinstance(self.direction, MatchDirection):
            raise MasterDataSeedError("direction must be a canonical MatchDirection.")
        if not isinstance(self.layout, StadiumCourseLayout):
            raise MasterDataSeedError("layout must be a canonical StadiumCourseLayout.")


def _canonicalize_graph(
    *,
    umamusumes: tuple[UmamusumeSeed, ...],
    umamusume_variants: tuple[UmamusumeVariantSeed, ...],
    stadiums: tuple[StadiumSeed, ...],
    stadium_courses: tuple[StadiumCourseSeed, ...],
    require_seed_minimum: bool,
) -> tuple[
    tuple[UmamusumeSeed, ...],
    tuple[UmamusumeVariantSeed, ...],
    tuple[StadiumSeed, ...],
    tuple[StadiumCourseSeed, ...],
]:
    ordered_umamusumes = tuple(sorted(umamusumes, key=lambda item: item.external_id))
    ordered_variants = tuple(sorted(umamusume_variants, key=lambda item: item.external_id))
    ordered_stadiums = tuple(sorted(stadiums, key=lambda item: item.external_id))
    ordered_courses = tuple(sorted(stadium_courses, key=lambda item: item.external_id))
    if require_seed_minimum and (not ordered_umamusumes or not ordered_stadiums or not ordered_courses):
        raise MasterDataSeedError("Initial master seed requires Umamusume, Stadium, and StadiumCourse rows.")
    for field_name, rows in (
        ("umamusumes", ordered_umamusumes),
        ("umamusume_variants", ordered_variants),
        ("stadiums", ordered_stadiums),
        ("stadium_courses", ordered_courses),
    ):
        external_ids = tuple(item.external_id for item in rows)
        if len(set(external_ids)) != len(external_ids):
            raise MasterDataSeedError(f"{field_name} contains duplicate external_id values.")
    umamusume_ids = {item.external_id for item in ordered_umamusumes}
    if any(item.umamusume_external_id not in umamusume_ids for item in ordered_variants):
        raise MasterDataSeedError("Every Umamusume variant must reference a seeded Umamusume external_id.")
    stadium_ids = {item.external_id for item in ordered_stadiums}
    if any(item.stadium_external_id not in stadium_ids for item in ordered_courses):
        raise MasterDataSeedError("Every StadiumCourse must reference a seeded Stadium external_id.")
    course_keys = tuple(
        (item.stadium_external_id, item.surface, item.distance, item.direction, item.layout) for item in ordered_courses
    )
    if len(set(course_keys)) != len(course_keys):
        raise MasterDataSeedError("stadium_courses contains duplicate canonical course tuples.")
    return ordered_umamusumes, ordered_variants, ordered_stadiums, ordered_courses


def _master_data_checksum(
    *,
    umamusumes: tuple[UmamusumeSeed, ...],
    umamusume_variants: tuple[UmamusumeVariantSeed, ...],
    stadiums: tuple[StadiumSeed, ...],
    stadium_courses: tuple[StadiumCourseSeed, ...],
) -> str:
    payload = {
        "stadium_courses": [
            {
                "direction": item.direction.value,
                "distance": item.distance,
                "external_id": item.external_id,
                "layout": item.layout.value,
                "stadium_external_id": item.stadium_external_id,
                "surface": item.surface.value,
            }
            for item in stadium_courses
        ],
        "stadiums": [
            {"external_id": item.external_id, "name_jp": item.name_jp, "name_ko": item.name_ko} for item in stadiums
        ],
        "umamusume_variants": [
            {
                "external_id": item.external_id,
                "name_jp": item.name_jp,
                "name_ko": item.name_ko,
                "release_date": item.release_date.isoformat() if item.release_date is not None else None,
                "umamusume_external_id": item.umamusume_external_id,
            }
            for item in umamusume_variants
        ],
        "umamusumes": [
            {"external_id": item.external_id, "name_jp": item.name_jp, "name_ko": item.name_ko} for item in umamusumes
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class MasterDataSnapshot:
    umamusumes: tuple[UmamusumeSeed, ...]
    umamusume_variants: tuple[UmamusumeVariantSeed, ...]
    stadiums: tuple[StadiumSeed, ...]
    stadium_courses: tuple[StadiumCourseSeed, ...]
    master_data_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        graph = _canonicalize_graph(
            umamusumes=tuple(self.umamusumes),
            umamusume_variants=tuple(self.umamusume_variants),
            stadiums=tuple(self.stadiums),
            stadium_courses=tuple(self.stadium_courses),
            require_seed_minimum=False,
        )
        object.__setattr__(self, "umamusumes", graph[0])
        object.__setattr__(self, "umamusume_variants", graph[1])
        object.__setattr__(self, "stadiums", graph[2])
        object.__setattr__(self, "stadium_courses", graph[3])
        object.__setattr__(
            self,
            "master_data_checksum",
            _master_data_checksum(
                umamusumes=graph[0],
                umamusume_variants=graph[1],
                stadiums=graph[2],
                stadium_courses=graph[3],
            ),
        )

    @property
    def is_empty(self) -> bool:
        return not (self.umamusumes or self.umamusume_variants or self.stadiums or self.stadium_courses)

    @property
    def counts(self) -> tuple[int, int, int, int]:
        return (
            len(self.umamusumes),
            len(self.umamusume_variants),
            len(self.stadiums),
            len(self.stadium_courses),
        )


@dataclass(frozen=True, slots=True)
class SeedMasterData:
    source_identifier: str
    manifest_checksum: str
    umamusumes: tuple[UmamusumeSeed, ...]
    umamusume_variants: tuple[UmamusumeVariantSeed, ...]
    stadiums: tuple[StadiumSeed, ...]
    stadium_courses: tuple[StadiumCourseSeed, ...]
    master_data_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_identifier",
            _required_text(self.source_identifier, field_name="source_identifier", max_length=200),
        )
        object.__setattr__(
            self,
            "manifest_checksum",
            _sha256_hex(self.manifest_checksum, field_name="manifest_checksum"),
        )
        graph = _canonicalize_graph(
            umamusumes=tuple(self.umamusumes),
            umamusume_variants=tuple(self.umamusume_variants),
            stadiums=tuple(self.stadiums),
            stadium_courses=tuple(self.stadium_courses),
            require_seed_minimum=True,
        )
        object.__setattr__(self, "umamusumes", graph[0])
        object.__setattr__(self, "umamusume_variants", graph[1])
        object.__setattr__(self, "stadiums", graph[2])
        object.__setattr__(self, "stadium_courses", graph[3])
        object.__setattr__(
            self,
            "master_data_checksum",
            _master_data_checksum(
                umamusumes=graph[0],
                umamusume_variants=graph[1],
                stadiums=graph[2],
                stadium_courses=graph[3],
            ),
        )

    @property
    def snapshot(self) -> MasterDataSnapshot:
        return MasterDataSnapshot(
            umamusumes=self.umamusumes,
            umamusume_variants=self.umamusume_variants,
            stadiums=self.stadiums,
            stadium_courses=self.stadium_courses,
        )


@dataclass(frozen=True, slots=True)
class MasterDataSeedReceipt:
    source_identifier: str
    manifest_checksum: str
    master_data_checksum: str
    umamusume_count: int
    umamusume_variant_count: int
    stadium_count: int
    stadium_course_count: int
    created: bool


class MasterDataSeedRepository(Protocol):
    def lock_current(self) -> MasterDataSnapshot: ...

    def create(self, *, command: SeedMasterData, created_at: datetime) -> None: ...


class MasterDataSeedUnitOfWork(UnitOfWork, Protocol):
    @property
    def master_data_seed(self) -> MasterDataSeedRepository: ...


class MasterDataSeedCommands:
    """Seed one complete reviewed initial master graph."""

    def __init__(
        self,
        runner: CommandRunner[MasterDataSeedUnitOfWork],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._runner = runner
        self._clock = clock

    def seed(self, command: SeedMasterData) -> MasterDataSeedReceipt:
        def operation(unit_of_work: MasterDataSeedUnitOfWork) -> MasterDataSeedReceipt:
            current = unit_of_work.master_data_seed.lock_current()
            requested = command.snapshot
            if current.is_empty:
                unit_of_work.master_data_seed.create(command=command, created_at=self._clock())
                return _receipt(command, created=True)
            if current != requested:
                raise MasterDataSeedConflictError(
                    "Current master data is partial or differs from the reviewed complete seed graph."
                )
            return _receipt(command, created=False)

        return self._runner.run(operation)


def _receipt(command: SeedMasterData, *, created: bool) -> MasterDataSeedReceipt:
    return MasterDataSeedReceipt(
        source_identifier=command.source_identifier,
        manifest_checksum=command.manifest_checksum,
        master_data_checksum=command.master_data_checksum,
        umamusume_count=len(command.umamusumes),
        umamusume_variant_count=len(command.umamusume_variants),
        stadium_count=len(command.stadiums),
        stadium_course_count=len(command.stadium_courses),
        created=created,
    )
