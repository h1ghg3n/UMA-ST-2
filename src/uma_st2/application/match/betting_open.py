"""Native V2 Match betting-open command boundary."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, UnitOfWork
from uma_st2.application.publication import PublicationIntent
from uma_st2.application.publication.match import (
    MatchOpeningCondition,
    MatchOpeningCourse,
    MatchOpeningEntry,
    MatchOpeningMarket,
    MatchOpeningPublicationSource,
    MatchPublicationDestination,
    build_match_opening_publication_intent,
    build_zero_pool_opening_markets,
)
from uma_st2.domain.betting import BetType
from uma_st2.domain.match import MATCH_ENTRY_MAXIMUM_COUNT, MatchGrade, MatchSourceKind, MatchStatus
from uma_st2.domain.publication import PublicationStatus
from uma_st2.shared import normalize_utc_datetime

MATCH_BETTING_OPEN_MINIMUM_ENTRY_COUNT: Final = 2
MATCH_BETTING_OPEN_AUDIT_SCHEMA_VERSION: Final = 1


class MatchBettingOpenAuditType(StrEnum):
    """Canonical operation type for the opening transition."""

    OPENED = "match_betting_opened"


class MatchBettingOpenError(ValueError):
    """Base error for rejected betting-open commands."""


class MatchBettingOpenUnavailableError(MatchBettingOpenError):
    """The target is absent or not a native scheduled Match."""


class MatchBettingOpenIncompleteError(MatchBettingOpenError):
    """Required condition or Entry facts are incomplete."""


class MatchBettingOpenInvalidSourceError(MatchBettingOpenError):
    """Persisted source facts violate the pre-open contract."""


class MatchBettingOpenStaleError(MatchBettingOpenError):
    """The final authority changed after the private Preview."""


class MatchBettingOpenIdempotencyConflictError(MatchBettingOpenError):
    """An idempotency key is bound to another logical command."""


class MatchBettingOpenAuditError(MatchBettingOpenError):
    """Stored exact-retry evidence is absent or malformed."""


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _normalized_string(
    value: str | None,
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


def _fingerprint(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _payload_positive_int(payload: Mapping[str, object], key: str) -> int:
    value = payload[key]
    _require_positive_int(value, field_name=key)  # type: ignore[arg-type]
    return value  # type: ignore[return-value]


def _payload_string(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string.")
    return value


def _payload_optional_string(payload: Mapping[str, object], key: str) -> str | None:
    value = payload[key]
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null.")
    return value


@dataclass(frozen=True, slots=True)
class MatchBettingOpenRatingRuleCoverage:
    """Closed-session facts about the current Rating rule authority."""

    current_version_available: bool
    current_version_complete: bool
    covered_converted_ranks: tuple[int, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.current_version_available, bool):
            raise ValueError("current_version_available must be a boolean.")
        if not isinstance(self.current_version_complete, bool):
            raise ValueError("current_version_complete must be a boolean.")
        if self.current_version_complete and not self.current_version_available:
            raise ValueError("A complete Rating rule version must be available.")
        ranks = tuple(self.covered_converted_ranks)
        if any(isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0 for rank in ranks):
            raise ValueError("covered_converted_ranks must contain positive integers.")
        if tuple(sorted(set(ranks))) != ranks:
            raise ValueError("covered_converted_ranks must be unique and ascending.")
        object.__setattr__(self, "covered_converted_ranks", ranks)


def match_betting_open_rating_rule_readiness_issue(
    *,
    grade: MatchGrade,
    field_size: int,
    coverage: MatchBettingOpenRatingRuleCoverage,
) -> str | None:
    if grade is MatchGrade.OP:
        return None
    if not coverage.current_version_available:
        return "현재 Rating rule version이 없습니다."
    if not coverage.current_version_complete:
        return "현재 Rating rule version이 완전하지 않습니다."
    if grade is MatchGrade.LISTED:
        return None
    if coverage.covered_converted_ranks != tuple(range(1, field_size + 1)):
        return f"현재 Rating rule version에 {grade.value} · Entry {field_size}명 규칙이 완전하지 않습니다."
    return None


@dataclass(frozen=True, slots=True)
class MatchBettingOpenTarget:
    """Closed-session current Match authority used by Preview and final command."""

    match_id: int
    match_name: str
    description: str | None
    source_kind: MatchSourceKind
    status: MatchStatus
    grade: MatchGrade
    scheduled_at: datetime
    course: MatchOpeningCourse
    condition: MatchOpeningCondition | None
    rating_rule_coverage: MatchBettingOpenRatingRuleCoverage
    entries: tuple[MatchOpeningEntry, ...] = field(default_factory=tuple)
    destination: MatchPublicationDestination | None = None
    state_fingerprint: str = ""

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(
            self,
            "description",
            _normalized_string(
                self.description,
                field_name="description",
                max_length=4000,
                optional=True,
            ),
        )
        object.__setattr__(self, "source_kind", MatchSourceKind(self.source_kind))
        object.__setattr__(self, "status", MatchStatus(self.status))
        object.__setattr__(self, "grade", MatchGrade(self.grade))
        object.__setattr__(
            self,
            "scheduled_at",
            normalize_utc_datetime(self.scheduled_at, field_name="scheduled_at"),
        )
        if not isinstance(self.course, MatchOpeningCourse):
            raise ValueError("course must be MatchOpeningCourse.")
        if self.condition is not None and not isinstance(self.condition, MatchOpeningCondition):
            raise ValueError("condition must be MatchOpeningCondition or null.")
        if not isinstance(self.rating_rule_coverage, MatchBettingOpenRatingRuleCoverage):
            raise ValueError("rating_rule_coverage must be MatchBettingOpenRatingRuleCoverage.")
        entries = tuple(sorted(self.entries, key=lambda entry: entry.entry_number))
        if entries and tuple(entry.entry_number for entry in entries) != tuple(range(1, len(entries) + 1)):
            raise ValueError("Match opening Entries must be contiguous from 1.")
        if len({entry.entry_id for entry in entries}) != len(entries):
            raise ValueError("Match opening Entry IDs must be unique.")
        object.__setattr__(self, "entries", entries)
        if self.destination is not None and not isinstance(self.destination, MatchPublicationDestination):
            raise ValueError("destination must be MatchPublicationDestination or null.")
        calculated = _fingerprint(self._state_payload())
        if self.state_fingerprint and self.state_fingerprint != calculated:
            raise ValueError("state_fingerprint does not match the Match opening authority.")
        object.__setattr__(self, "state_fingerprint", calculated)

    @property
    def readiness_issues(self) -> tuple[str, ...]:
        issues: list[str] = []
        if self.source_kind != MatchSourceKind.NATIVE_V2:
            issues.append("native V2 Match가 아닙니다.")
        if self.status != MatchStatus.SCHEDULED:
            issues.append("scheduled 상태가 아닙니다.")
        if self.condition is None:
            issues.append("환경 조건이 확정되지 않았습니다.")
        entry_count = len(self.entries)
        if entry_count < MATCH_BETTING_OPEN_MINIMUM_ENTRY_COUNT:
            issues.append(f"Entry가 최소 {MATCH_BETTING_OPEN_MINIMUM_ENTRY_COUNT}명 필요합니다. (현재 {entry_count}명)")
        elif entry_count > MATCH_ENTRY_MAXIMUM_COUNT:
            issues.append(f"Entry는 최대 {MATCH_ENTRY_MAXIMUM_COUNT}명까지 허용됩니다. (현재 {entry_count}명)")
        else:
            rating_issue = match_betting_open_rating_rule_readiness_issue(
                grade=self.grade,
                field_size=entry_count,
                coverage=self.rating_rule_coverage,
            )
            if rating_issue is not None:
                issues.append(rating_issue)
        if self.destination is None:
            issues.append("Discord guild publication setting을 읽지 못했습니다.")
        return tuple(issues)

    @property
    def markets(self) -> tuple[MatchOpeningMarket, ...]:
        if not MATCH_BETTING_OPEN_MINIMUM_ENTRY_COUNT <= len(self.entries) <= MATCH_ENTRY_MAXIMUM_COUNT:
            return ()
        return build_zero_pool_opening_markets(len(self.entries))

    def to_publication_source(self) -> MatchOpeningPublicationSource:
        if self.readiness_issues:
            raise ValueError("An incomplete Match opening target cannot produce a publication source.")
        condition = self.condition
        destination = self.destination
        if condition is None or destination is None:  # pragma: no cover - readiness invariant
            raise ValueError("An incomplete Match opening target cannot produce a publication source.")
        return MatchOpeningPublicationSource(
            destination=destination,
            match_id=self.match_id,
            match_name=self.match_name,
            description=self.description,
            grade=self.grade,
            scheduled_at=self.scheduled_at,
            course=self.course,
            condition=condition,
            entries=self.entries,
            markets=self.markets,
        )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": MATCH_BETTING_OPEN_AUDIT_SCHEMA_VERSION,
            **self._state_payload(),
            "state_fingerprint": self.state_fingerprint,
        }

    def _state_payload(self) -> dict[str, object]:
        return {
            "match_id": self.match_id,
            "match_name": self.match_name,
            "description": self.description,
            "source_kind": self.source_kind.value,
            "status": self.status.value,
            "grade": self.grade.value,
            "scheduled_at": self.scheduled_at.isoformat(),
            "course": self.course.to_payload(),
            "condition": self.condition.to_payload() if self.condition is not None else None,
            "entries": [entry.to_payload() for entry in self.entries],
            "destination": self.destination.to_payload() if self.destination is not None else None,
        }


@dataclass(frozen=True, slots=True)
class StoredMatchOpeningPublication:
    """Inserted durable publication identity returned inside the transaction."""

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
        if len(self.payload_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.payload_fingerprint
        ):
            raise ValueError("payload_fingerprint must be a lowercase SHA-256 digest.")
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
    def from_audit_payload(cls, payload: Mapping[str, object]) -> StoredMatchOpeningPublication:
        return cls(
            publication_id=_payload_positive_int(payload, "publication_id"),
            event_key=_payload_string(payload, "event_key"),
            payload_fingerprint=_payload_string(payload, "payload_fingerprint"),
            status=PublicationStatus(_payload_string(payload, "status")),
            target_channel_id=_payload_optional_string(payload, "target_channel_id"),
        )


@dataclass(frozen=True, slots=True)
class OpenedMatchBetting:
    """Committed opening receipt returned by save or exact retry."""

    match_id: int
    match_name: str
    status: MatchStatus
    opened_at: datetime
    entry_count: int
    markets: tuple[MatchOpeningMarket, ...]
    publication: StoredMatchOpeningPublication

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "status", MatchStatus(self.status))
        if self.status != MatchStatus.BETTING_OPEN:
            raise ValueError("An opened Match receipt must have betting_open status.")
        object.__setattr__(
            self,
            "opened_at",
            normalize_utc_datetime(self.opened_at, field_name="opened_at"),
        )
        _require_positive_int(self.entry_count, field_name="entry_count")
        markets = tuple(self.markets)
        if tuple(market.bet_type for market in markets) != tuple(BetType):
            raise ValueError("An opened Match receipt must include every market availability row.")
        object.__setattr__(self, "markets", markets)
        if not isinstance(self.publication, StoredMatchOpeningPublication):
            raise ValueError("publication must be StoredMatchOpeningPublication.")

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": MATCH_BETTING_OPEN_AUDIT_SCHEMA_VERSION,
            "match_id": self.match_id,
            "match_name": self.match_name,
            "status": self.status.value,
            "opened_at": self.opened_at.isoformat(),
            "entry_count": self.entry_count,
            "markets": [market.to_payload() for market in self.markets],
            "publication": self.publication.to_audit_payload(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> OpenedMatchBetting:
        if payload.get("schema_version") != MATCH_BETTING_OPEN_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported Match betting-open audit schema version.")
        raw_markets = payload["markets"]
        raw_publication = payload["publication"]
        if not isinstance(raw_markets, list) or not all(isinstance(item, Mapping) for item in raw_markets):
            raise ValueError("markets must be a list of objects.")
        if not isinstance(raw_publication, Mapping):
            raise ValueError("publication must be an object.")
        markets = tuple(
            MatchOpeningMarket(
                bet_type=BetType(_payload_string(item, "bet_type")),
                available=item["available"],  # type: ignore[arg-type]
                selection_count=item["selection_count"],  # type: ignore[arg-type]
                uniform_odds=Decimal(value) if (value := item["uniform_odds"]) is not None else None,
            )
            for item in raw_markets
        )
        return cls(
            match_id=_payload_positive_int(payload, "match_id"),
            match_name=_payload_string(payload, "match_name"),
            status=MatchStatus(_payload_string(payload, "status")),
            opened_at=datetime.fromisoformat(_payload_string(payload, "opened_at")),
            entry_count=_payload_positive_int(payload, "entry_count"),
            markets=markets,
            publication=StoredMatchOpeningPublication.from_audit_payload(raw_publication),
        )


@dataclass(frozen=True, slots=True)
class OpenMatchBetting:
    """Final-confirm command for one previewed native Match."""

    match_id: int
    expected_state_fingerprint: str
    idempotency_key: str
    actor_discord_user_id: str
    guild_id: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        if len(self.expected_state_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.expected_state_fingerprint
        ):
            raise ValueError("expected_state_fingerprint must be a lowercase SHA-256 digest.")
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
                "schema": "match-betting-open-command-v1",
                "match_id": self.match_id,
                "guild_id": self.guild_id,
                "expected_state_fingerprint": self.expected_state_fingerprint,
            }
        )


@dataclass(frozen=True, slots=True)
class StoredMatchBettingOpenOperation:
    """Minimal operation record used to resolve exact retry."""

    request_fingerprint: str | None
    type: str | None
    match_id: int | None
    after_data: Mapping[str, object] | None


class MatchBettingOpenRepository(Protocol):
    """Persistence operations for one atomic opening transition."""

    def lock_target(self, *, match_id: int, guild_id: str) -> MatchBettingOpenTarget | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredMatchBettingOpenOperation | None: ...

    def has_any_bet_facts(self, *, match_id: int) -> bool: ...

    def transition_to_betting_open(self, *, match_id: int, changed_at: datetime) -> None: ...

    def start_periodic_odds_cycle_if_first_open(
        self,
        *,
        match_id: int,
        guild_id: str,
        opened_at: datetime,
    ) -> None: ...

    def add_publication(
        self,
        *,
        intent: PublicationIntent,
        created_at: datetime,
    ) -> StoredMatchOpeningPublication: ...

    def add_audit(
        self,
        *,
        command: OpenMatchBetting,
        before: MatchBettingOpenTarget,
        after: OpenedMatchBetting,
        created_at: datetime,
    ) -> None: ...


class MatchBettingOpenUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only the opening command repository."""

    @property
    def match_betting_open(self) -> MatchBettingOpenRepository: ...


@dataclass(frozen=True, slots=True)
class MatchBettingOpenCommands:
    """Application entry point for native scheduled -> betting_open."""

    command_runner: CommandRunner[MatchBettingOpenUnitOfWork]
    clock: Callable[[], datetime]

    def open_betting(self, command: OpenMatchBetting) -> OpenedMatchBetting:
        return self.command_runner.run(lambda unit_of_work: self._open(unit_of_work.match_betting_open, command))

    def _open(
        self,
        repository: MatchBettingOpenRepository,
        command: OpenMatchBetting,
    ) -> OpenedMatchBetting:
        try:
            target = repository.lock_target(match_id=command.match_id, guild_id=command.guild_id)
        except (TypeError, ValueError) as exc:
            raise MatchBettingOpenInvalidSourceError("Stored Match opening facts are malformed.") from exc
        if target is None:
            raise MatchBettingOpenUnavailableError("Match does not exist.")

        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)

        if target.source_kind != MatchSourceKind.NATIVE_V2 or target.status != MatchStatus.SCHEDULED:
            raise MatchBettingOpenUnavailableError("Betting open requires a native scheduled Match.")
        if target.state_fingerprint != command.expected_state_fingerprint:
            raise MatchBettingOpenStaleError("Match opening facts changed after Preview.")
        if target.readiness_issues:
            raise MatchBettingOpenIncompleteError(" ".join(target.readiness_issues))
        if repository.has_any_bet_facts(match_id=target.match_id):
            raise MatchBettingOpenInvalidSourceError("A scheduled Match contains a pre-open Bet fact.")

        opened_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        try:
            source = target.to_publication_source()
            intent = build_match_opening_publication_intent(source)
            repository.transition_to_betting_open(match_id=target.match_id, changed_at=opened_at)
            repository.start_periodic_odds_cycle_if_first_open(
                match_id=target.match_id,
                guild_id=command.guild_id,
                opened_at=opened_at,
            )
            publication = repository.add_publication(intent=intent, created_at=opened_at)
            after = OpenedMatchBetting(
                match_id=target.match_id,
                match_name=target.match_name,
                status=MatchStatus.BETTING_OPEN,
                opened_at=opened_at,
                entry_count=len(target.entries),
                markets=target.markets,
                publication=publication,
            )
        except MatchBettingOpenError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise MatchBettingOpenInvalidSourceError(
                "Match opening did not produce complete canonical evidence."
            ) from exc
        repository.add_audit(command=command, before=target, after=after, created_at=opened_at)
        return after

    @staticmethod
    def _resolve_exact_retry(
        *,
        stored: StoredMatchBettingOpenOperation,
        command: OpenMatchBetting,
    ) -> OpenedMatchBetting:
        if (
            stored.type != MatchBettingOpenAuditType.OPENED.value
            or stored.request_fingerprint != command.request_fingerprint
            or stored.match_id != command.match_id
        ):
            raise MatchBettingOpenIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise MatchBettingOpenAuditError("Exact-retry operation has no stored after snapshot.")
        try:
            opened = OpenedMatchBetting.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise MatchBettingOpenAuditError("Exact-retry operation has malformed stored evidence.") from exc
        if opened.match_id != command.match_id:
            raise MatchBettingOpenAuditError("Exact-retry snapshot belongs to another Match.")
        return opened
