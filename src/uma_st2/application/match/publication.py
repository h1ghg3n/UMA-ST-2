"""Explicit post-settlement native Match result publication boundary."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, QueryRunner, UnitOfWork
from uma_st2.application.publication import PublicationIntent
from uma_st2.application.publication.match import (
    MatchOpeningCondition,
    MatchPublicationDestination,
    MatchResultCourse,
    MatchResultEntry,
    MatchResultOdds,
    MatchResultPublicationSource,
    build_match_result_publication_intent,
)
from uma_st2.domain.match import MatchGrade, MatchSourceKind, MatchStatus
from uma_st2.domain.publication import PublicationStatus
from uma_st2.shared import normalize_utc_datetime

from .settlement import SettledMatch

MATCH_RESULT_PUBLICATION_AUDIT_SCHEMA_VERSION: Final = 1


class MatchResultPublicationAuditType(StrEnum):
    """Canonical operation type for explicit missing-result-intent recovery."""

    PUBLISHED = "room_result_publish"


class MatchResultPublicationError(ValueError):
    """Base error for rejected Match result publication."""


class MatchResultPublicationUnavailableError(MatchResultPublicationError):
    """The Match is absent or not a native settled publication target."""


class MatchResultPublicationAlreadyExistsError(MatchResultPublicationError):
    """Another command already created the one logical result publication."""


class MatchResultPublicationInvalidSourceError(MatchResultPublicationError):
    """Stored Match or settlement publication authority is malformed."""


class MatchResultPublicationIdempotencyConflictError(MatchResultPublicationError):
    """An idempotency key is bound to another logical operation."""


class MatchResultPublicationAuditError(MatchResultPublicationError):
    """Stored exact-retry publication evidence is absent or malformed."""


def _require_positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")
    return value


def _normalized_string(
    value: object,
    *,
    field_name: str,
    max_length: int,
    optional: bool = False,
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")
    normalized = value.strip()
    if optional and not normalized:
        return None
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field_name} must be a non-empty string of at most {max_length} characters.")
    return normalized


def _sha256_hex(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a SHA-256 hex string.")
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError(f"{field_name} must be a SHA-256 hex string.")
    return normalized


def _fingerprint(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _payload_mapping(payload: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = payload[key]
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be an object.")
    return value


def _payload_string(payload: Mapping[str, object], key: str, *, max_length: int = 255) -> str:
    value = _normalized_string(payload[key], field_name=key, max_length=max_length)
    assert value is not None
    return value


def _payload_optional_string(payload: Mapping[str, object], key: str, *, max_length: int) -> str | None:
    return _normalized_string(payload.get(key), field_name=key, max_length=max_length, optional=True)


@dataclass(frozen=True, slots=True)
class MatchResultPublicationLock:
    """Minimal Match-first lock read before exact retry and eligibility checks."""

    match_id: int
    match_name: str
    source_kind: MatchSourceKind
    status: MatchStatus

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "source_kind", MatchSourceKind(self.source_kind))
        object.__setattr__(self, "status", MatchStatus(self.status))


@dataclass(frozen=True, slots=True)
class MatchResultPublicationTarget:
    """Complete closed-session authority used to build the public snapshot."""

    match_id: int
    match_name: str
    source_kind: MatchSourceKind
    status: MatchStatus
    grade: MatchGrade
    scheduled_at: datetime
    course: MatchResultCourse
    condition: MatchOpeningCondition
    destination: MatchPublicationDestination
    settlement: SettledMatch

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "source_kind", MatchSourceKind(self.source_kind))
        object.__setattr__(self, "status", MatchStatus(self.status))
        object.__setattr__(self, "grade", MatchGrade(self.grade))
        object.__setattr__(
            self,
            "scheduled_at",
            normalize_utc_datetime(self.scheduled_at, field_name="scheduled_at"),
        )
        if not isinstance(self.course, MatchResultCourse):
            raise ValueError("course must be MatchResultCourse.")
        if not isinstance(self.condition, MatchOpeningCondition):
            raise ValueError("condition must be MatchOpeningCondition.")
        if not isinstance(self.destination, MatchPublicationDestination):
            raise ValueError("destination must be MatchPublicationDestination.")
        if not isinstance(self.settlement, SettledMatch):
            raise ValueError("settlement must be SettledMatch.")
        if (
            self.settlement.match_id != self.match_id
            or self.settlement.match_name != self.match_name
            or self.settlement.grade is not self.grade
        ):
            raise ValueError("Settlement evidence does not match the current Match identity.")

    def to_publication_source(self) -> MatchResultPublicationSource:
        return MatchResultPublicationSource(
            destination=self.destination,
            match_id=self.match_id,
            match_name=self.match_name,
            grade=self.grade,
            scheduled_at=self.scheduled_at,
            course=self.course,
            condition=self.condition,
            entries=tuple(
                MatchResultEntry(
                    entry_number=rating.entry_number,
                    official_rank=rating.rank,
                    player_name=rating.game_account_name,
                    character_name=rating.horse_name,
                    affiliation=rating.affiliation_at_event,
                    rating_before=rating.rating_before,
                    rating_delta=rating.amount,
                    rating_after=rating.rating_after,
                    rating_disposition=rating.rating_disposition,
                    rating_rank=rating.rating_rank,
                )
                for rating in self.settlement.ratings
            ),
            odds=tuple(
                MatchResultOdds(
                    bet_type=odds.bet_type,
                    selection_entry_numbers=odds.selection_entry_numbers,
                    confirmed_odds=odds.confirmed_odds,
                )
                for odds in self.settlement.applied_odds
            ),
        )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": MATCH_RESULT_PUBLICATION_AUDIT_SCHEMA_VERSION,
            "match_id": self.match_id,
            "match_name": self.match_name,
            "source_kind": self.source_kind.value,
            "status": self.status.value,
            "grade": self.grade.value,
            "settlement_fingerprint": self.settlement.settlement_fingerprint,
            "destination": self.destination.to_payload(),
        }


@dataclass(frozen=True, slots=True)
class StoredMatchResultPublication:
    """Inserted durable result publication identity."""

    publication_id: int
    event_key: str
    payload_fingerprint: str
    status: PublicationStatus
    target_channel_id: str | None

    def __post_init__(self) -> None:
        _require_positive_int(self.publication_id, field_name="publication_id")
        object.__setattr__(
            self,
            "event_key",
            _normalized_string(self.event_key, field_name="event_key", max_length=128),
        )
        object.__setattr__(
            self,
            "payload_fingerprint",
            _sha256_hex(self.payload_fingerprint, field_name="payload_fingerprint"),
        )
        object.__setattr__(self, "status", PublicationStatus(self.status))
        object.__setattr__(
            self,
            "target_channel_id",
            _normalized_string(
                self.target_channel_id,
                field_name="target_channel_id",
                max_length=32,
                optional=True,
            ),
        )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "publication_id": self.publication_id,
            "event_key": self.event_key,
            "payload_fingerprint": self.payload_fingerprint,
            "status": self.status.value,
            "target_channel_id": self.target_channel_id,
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> StoredMatchResultPublication:
        return cls(
            publication_id=_require_positive_int(payload["publication_id"], field_name="publication_id"),
            event_key=_payload_string(payload, "event_key", max_length=128),
            payload_fingerprint=_sha256_hex(payload["payload_fingerprint"], field_name="payload_fingerprint"),
            status=PublicationStatus(_payload_string(payload, "status", max_length=32)),
            target_channel_id=_payload_optional_string(payload, "target_channel_id", max_length=32),
        )


@dataclass(frozen=True, slots=True)
class PublishedMatchResult:
    """Committed private receipt returned by save or exact retry."""

    match_id: int
    match_name: str
    match_status: MatchStatus
    settlement_fingerprint: str
    intent_created_at: datetime
    publication: StoredMatchResultPublication

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "match_status", MatchStatus(self.match_status))
        if self.match_status is not MatchStatus.SETTLED:
            raise ValueError("A result publication receipt requires a settled Match.")
        object.__setattr__(
            self,
            "settlement_fingerprint",
            _sha256_hex(self.settlement_fingerprint, field_name="settlement_fingerprint"),
        )
        object.__setattr__(
            self,
            "intent_created_at",
            normalize_utc_datetime(self.intent_created_at, field_name="intent_created_at"),
        )
        if not isinstance(self.publication, StoredMatchResultPublication):
            raise ValueError("publication must be StoredMatchResultPublication.")

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": MATCH_RESULT_PUBLICATION_AUDIT_SCHEMA_VERSION,
            "match_id": self.match_id,
            "match_name": self.match_name,
            "match_status": self.match_status.value,
            "settlement_fingerprint": self.settlement_fingerprint,
            "intent_created_at": self.intent_created_at.isoformat(),
            "publication": self.publication.to_audit_payload(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> PublishedMatchResult:
        if payload.get("schema_version") != MATCH_RESULT_PUBLICATION_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported Match result publication audit schema version.")
        return cls(
            match_id=_require_positive_int(payload["match_id"], field_name="match_id"),
            match_name=_payload_string(payload, "match_name", max_length=200),
            match_status=MatchStatus(_payload_string(payload, "match_status", max_length=32)),
            settlement_fingerprint=_sha256_hex(
                payload["settlement_fingerprint"],
                field_name="settlement_fingerprint",
            ),
            intent_created_at=datetime.fromisoformat(_payload_string(payload, "intent_created_at")),
            publication=StoredMatchResultPublication.from_audit_payload(_payload_mapping(payload, "publication")),
        )


@dataclass(frozen=True, slots=True)
class PublishMatchResult:
    """Explicit one-shot command creating post-settlement public intent."""

    match_id: int
    idempotency_key: str
    actor_discord_user_id: str
    guild_id: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        for field_name, max_length, optional in (
            ("idempotency_key", 128, False),
            ("actor_discord_user_id", 32, False),
            ("guild_id", 32, False),
            ("correlation_id", 128, True),
        ):
            object.__setattr__(
                self,
                field_name,
                _normalized_string(
                    getattr(self, field_name),
                    field_name=field_name,
                    max_length=max_length,
                    optional=optional,
                ),
            )

    @property
    def request_fingerprint(self) -> str:
        return _fingerprint(
            {
                "schema": "match-result-publication-command-v1",
                "match_id": self.match_id,
                "guild_id": self.guild_id,
            }
        )


@dataclass(frozen=True, slots=True)
class StoredMatchResultPublicationOperation:
    request_fingerprint: str | None
    type: str | None
    match_id: int | None
    after_data: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class MatchResultPublicationTargetChoice:
    match_id: int
    match_name: str
    grade: MatchGrade

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "grade", MatchGrade(self.grade))


class MatchResultPublicationRepository(Protocol):
    def lock_match(self, *, match_id: int) -> MatchResultPublicationLock | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredMatchResultPublicationOperation | None: ...

    def find_result_publication(self, *, match_id: int) -> StoredMatchResultPublication | None: ...

    def load_target(self, *, match_id: int, guild_id: str) -> MatchResultPublicationTarget | None: ...

    def add_publication(
        self,
        *,
        intent: PublicationIntent,
        created_at: datetime,
    ) -> StoredMatchResultPublication: ...

    def add_audit(
        self,
        *,
        command: PublishMatchResult,
        before: MatchResultPublicationTarget,
        after: PublishedMatchResult,
        created_at: datetime,
    ) -> None: ...


class MatchResultPublicationQueryRepository(Protocol):
    def search_targets(self, *, search: str, limit: int) -> tuple[MatchResultPublicationTargetChoice, ...]: ...


class MatchResultPublicationUnitOfWork(UnitOfWork, Protocol):
    @property
    def match_result_publication(self) -> MatchResultPublicationRepository: ...


class MatchResultPublicationQueryUnitOfWork(UnitOfWork, Protocol):
    @property
    def match_result_publication_queries(self) -> MatchResultPublicationQueryRepository: ...


@dataclass(frozen=True, slots=True)
class MatchResultPublicationQueries:
    query_runner: QueryRunner[MatchResultPublicationQueryUnitOfWork]

    def search_targets(self, *, search: str, limit: int = 25) -> tuple[MatchResultPublicationTargetChoice, ...]:
        if not isinstance(search, str):
            raise ValueError("search must be a string.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be between 1 and 25.")
        normalized = search.strip()
        return self.query_runner.run(
            lambda unit_of_work: unit_of_work.match_result_publication_queries.search_targets(
                search=normalized,
                limit=limit,
            )
        )


@dataclass(frozen=True, slots=True)
class MatchResultPublicationCommands:
    command_runner: CommandRunner[MatchResultPublicationUnitOfWork]
    clock: Callable[[], datetime]

    def publish_result(self, command: PublishMatchResult) -> PublishedMatchResult:
        return self.command_runner.run(
            lambda unit_of_work: self._publish(unit_of_work.match_result_publication, command)
        )

    def _publish(
        self,
        repository: MatchResultPublicationRepository,
        command: PublishMatchResult,
    ) -> PublishedMatchResult:
        try:
            locked = repository.lock_match(match_id=command.match_id)
        except (TypeError, ValueError) as exc:
            raise MatchResultPublicationInvalidSourceError("Stored Match publication lock is malformed.") from exc
        if locked is None:
            raise MatchResultPublicationUnavailableError("Match does not exist.")

        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)
        if locked.source_kind is not MatchSourceKind.NATIVE_V2 or locked.status is not MatchStatus.SETTLED:
            raise MatchResultPublicationUnavailableError("Publication requires a native settled Match.")
        if repository.find_result_publication(match_id=command.match_id) is not None:
            raise MatchResultPublicationAlreadyExistsError(
                "The logical settled-result publication already exists for this Match."
            )

        try:
            target = repository.load_target(match_id=command.match_id, guild_id=command.guild_id)
            if target is None:
                raise MatchResultPublicationUnavailableError("Match result publication target is unavailable.")
            if (
                target.match_id != locked.match_id
                or target.match_name != locked.match_name
                or target.source_kind is not locked.source_kind
                or target.status is not locked.status
            ):
                raise ValueError("Locked Match identity changed while building the publication target.")
            intent = build_match_result_publication_intent(target.to_publication_source())
        except MatchResultPublicationError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise MatchResultPublicationInvalidSourceError(
                "Stored settlement publication authority is malformed."
            ) from exc

        created_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        try:
            publication = repository.add_publication(intent=intent, created_at=created_at)
            result = PublishedMatchResult(
                match_id=target.match_id,
                match_name=target.match_name,
                match_status=target.status,
                settlement_fingerprint=target.settlement.settlement_fingerprint,
                intent_created_at=created_at,
                publication=publication,
            )
        except MatchResultPublicationError:
            raise
        except (TypeError, ValueError) as exc:
            raise MatchResultPublicationInvalidSourceError(
                "Match result publication did not produce complete durable evidence."
            ) from exc
        repository.add_audit(command=command, before=target, after=result, created_at=created_at)
        return result

    @staticmethod
    def _resolve_exact_retry(
        *,
        stored: StoredMatchResultPublicationOperation,
        command: PublishMatchResult,
    ) -> PublishedMatchResult:
        if (
            stored.type != MatchResultPublicationAuditType.PUBLISHED.value
            or stored.request_fingerprint != command.request_fingerprint
            or stored.match_id != command.match_id
        ):
            raise MatchResultPublicationIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise MatchResultPublicationAuditError("Exact-retry publication has no stored evidence.")
        try:
            result = PublishedMatchResult.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise MatchResultPublicationAuditError("Exact-retry publication evidence is malformed.") from exc
        if result.match_id != command.match_id:
            raise MatchResultPublicationAuditError("Exact-retry publication belongs to another Match.")
        return result
