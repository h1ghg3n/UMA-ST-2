"""Immutable RatingRuleVersion seed command boundary."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol

from uma_st2.application.execution import CommandRunner, UnitOfWork
from uma_st2.domain.rating import RatingRule


class RatingRuleSeedError(ValueError):
    """A Rating rule seed request is malformed."""


class RatingRuleSeedConflictError(RatingRuleSeedError):
    """Stored source provenance conflicts with the requested canonical rules."""


def _required_text(value: object, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise RatingRuleSeedError(f"{field_name} must be text.")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length or any(ord(character) < 32 for character in normalized):
        raise RatingRuleSeedError(f"{field_name} must contain 1 to {max_length} printable characters.")
    return normalized


def _sha256_hex(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise RatingRuleSeedError(f"{field_name} must be a SHA-256 hex string.")
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise RatingRuleSeedError(f"{field_name} must be a SHA-256 hex string.")
    return normalized


@dataclass(frozen=True, slots=True)
class SeedRatingRuleVersion:
    """One reviewed workbook rule set to seed idempotently."""

    source_identifier: str
    source_checksum: str
    source_sheet_name: str
    source_range: str
    rules: tuple[RatingRule, ...]
    rule_set_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        source_identifier = _required_text(
            self.source_identifier,
            field_name="source_identifier",
            max_length=200,
        )
        source_checksum = _sha256_hex(self.source_checksum, field_name="source_checksum")
        source_sheet_name = _required_text(
            self.source_sheet_name,
            field_name="source_sheet_name",
            max_length=100,
        )
        source_range = _required_text(self.source_range, field_name="source_range", max_length=64)
        ordered = tuple(
            sorted(
                self.rules,
                key=lambda rule: (rule.grade.value, rule.participant_count, rule.converted_rank),
            )
        )
        if not ordered:
            raise RatingRuleSeedError("Rating rule seed requires at least one rule.")
        keys = tuple((rule.grade, rule.participant_count, rule.converted_rank) for rule in ordered)
        if len(set(keys)) != len(keys):
            raise RatingRuleSeedError("Rating rule seed contains duplicate grade/participant/rank keys.")
        canonical = [
            [rule.grade.value, rule.participant_count, rule.converted_rank, format(rule.base_delta, "f")]
            for rule in ordered
        ]
        object.__setattr__(self, "source_identifier", source_identifier)
        object.__setattr__(self, "source_checksum", source_checksum)
        object.__setattr__(self, "source_sheet_name", source_sheet_name)
        object.__setattr__(self, "source_range", source_range)
        object.__setattr__(self, "rules", ordered)
        object.__setattr__(
            self,
            "rule_set_checksum",
            sha256(json.dumps(canonical, ensure_ascii=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class StoredRatingRuleVersion:
    """Closed stored provenance used for idempotency comparison."""

    version_id: int
    version_number: int
    rule_set_checksum: str
    rule_count: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SeededRatingRuleVersion:
    """Committed immutable Rating rule version receipt."""

    version_id: int
    version_number: int
    rule_set_checksum: str
    rule_count: int
    created: bool
    created_at: datetime


class RatingRuleSeedRepository(Protocol):
    def find_source_version(self, *, command: SeedRatingRuleVersion) -> StoredRatingRuleVersion | None: ...

    def lock_latest_version_number(self) -> int: ...

    def create_version(
        self,
        *,
        command: SeedRatingRuleVersion,
        version_number: int,
        created_at: datetime,
    ) -> StoredRatingRuleVersion: ...


class RatingRuleSeedUnitOfWork(UnitOfWork, Protocol):
    @property
    def rating_rule_seed(self) -> RatingRuleSeedRepository: ...


class RatingRuleSeedCommands:
    """Seed reviewed Rating rules without exposing SQLAlchemy mechanics."""

    def __init__(
        self,
        runner: CommandRunner[RatingRuleSeedUnitOfWork],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._runner = runner
        self._clock = clock

    def seed(self, command: SeedRatingRuleVersion) -> SeededRatingRuleVersion:
        def operation(unit_of_work: RatingRuleSeedUnitOfWork) -> SeededRatingRuleVersion:
            repository = unit_of_work.rating_rule_seed
            existing = repository.find_source_version(command=command)
            if existing is not None:
                if existing.rule_set_checksum != command.rule_set_checksum or existing.rule_count != len(command.rules):
                    raise RatingRuleSeedConflictError(
                        "Stored RatingRule source provenance has different canonical rule content."
                    )
                return _receipt(existing, created=False)
            version_number = repository.lock_latest_version_number() + 1
            created = repository.create_version(
                command=command,
                version_number=version_number,
                created_at=self._clock(),
            )
            return _receipt(created, created=True)

        return self._runner.run(operation)


def _receipt(version: StoredRatingRuleVersion, *, created: bool) -> SeededRatingRuleVersion:
    return SeededRatingRuleVersion(
        version_id=version.version_id,
        version_number=version.version_number,
        rule_set_checksum=version.rule_set_checksum,
        rule_count=version.rule_count,
        created=created,
        created_at=version.created_at,
    )
