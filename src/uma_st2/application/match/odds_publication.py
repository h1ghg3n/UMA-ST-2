"""Guild-wide periodic native Match provisional-odds publication boundary."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, QueryRunner, UnitOfWork
from uma_st2.application.publication import PublicationIntent
from uma_st2.application.publication.match import MatchPublicationDestination
from uma_st2.application.publication.match_odds import (
    MatchOddsRefreshMatch,
    MatchOddsRefreshMode,
    MatchOddsRefreshPublicationSource,
    MatchOddsRefreshSelection,
    build_match_odds_refresh_publication_intent,
)
from uma_st2.domain.betting import BetPoolStake, calculate_provisional_odds
from uma_st2.domain.match import MatchGrade, MatchSourceKind, MatchStatus
from uma_st2.domain.publication import PublicationStatus
from uma_st2.shared import normalize_utc_datetime

MATCH_ODDS_MODE_AUDIT_SCHEMA_VERSION: Final = 1
MATCH_ODDS_MODE_OPERATION_TYPE: Final = "match_odds_refresh_mode_changed"


class MatchOddsPublicationError(ValueError):
    """Base error for rejected periodic Match odds operations."""


class MatchOddsPublicationUnavailableError(MatchOddsPublicationError):
    """Guild settings or an open Match target is unavailable."""


class MatchOddsPublicationNoChangeError(MatchOddsPublicationError):
    """The requested mode already owns the current cursor."""


class MatchOddsPublicationInvalidSourceError(MatchOddsPublicationError):
    """Stored current Match, Bet, or cursor facts are malformed."""


class MatchOddsPublicationIdempotencyConflictError(MatchOddsPublicationError):
    """The idempotency key belongs to another logical mode change."""


class MatchOddsPublicationAuditError(MatchOddsPublicationError):
    """Stored exact-retry evidence is incomplete or malformed."""


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")
    return value


def _non_negative_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")
    return value


def _text(value: object, *, field_name: str, max_length: int, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text.")
    normalized = value.strip()
    if optional and not normalized:
        return None
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field_name} must contain 1 to {max_length} characters.")
    return normalized


def _sha256(value: object, *, field_name: str, optional: bool = False) -> str | None:
    normalized = _text(value, field_name=field_name, max_length=64, optional=optional)
    if normalized is None:
        return None
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest.")
    return normalized


def _fingerprint(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MatchOddsRefreshCursor:
    """Durable guild-scoped publication cursor, not an odds snapshot."""

    guild_id: str
    mode: MatchOddsRefreshMode
    next_refresh_at: datetime | None
    sequence: int
    last_projection_fingerprint: str | None
    destination: MatchPublicationDestination

    def __post_init__(self) -> None:
        guild_id = _text(self.guild_id, field_name="guild_id", max_length=32)
        assert guild_id is not None
        object.__setattr__(self, "guild_id", guild_id)
        object.__setattr__(self, "mode", MatchOddsRefreshMode(self.mode))
        if self.next_refresh_at is not None:
            object.__setattr__(
                self,
                "next_refresh_at",
                normalize_utc_datetime(self.next_refresh_at, field_name="next_refresh_at"),
            )
        _non_negative_int(self.sequence, field_name="sequence")
        object.__setattr__(
            self,
            "last_projection_fingerprint",
            _sha256(
                self.last_projection_fingerprint,
                field_name="last_projection_fingerprint",
                optional=True,
            ),
        )
        if not isinstance(self.destination, MatchPublicationDestination):
            raise ValueError("destination must be MatchPublicationDestination.")
        if self.destination.guild_id != self.guild_id:
            raise ValueError("Cursor destination belongs to another guild.")

    def to_payload(self) -> dict[str, object]:
        return {
            "guild_id": self.guild_id,
            "mode": self.mode.value,
            "next_refresh_at": self.next_refresh_at.isoformat() if self.next_refresh_at else None,
            "sequence": self.sequence,
            "last_projection_fingerprint": self.last_projection_fingerprint,
            "destination": self.destination.to_payload(),
        }


@dataclass(frozen=True, slots=True)
class MatchOddsRefreshEntry:
    """Internal Entry identity mapped to its public operator number."""

    entry_id: int
    entry_number: int

    def __post_init__(self) -> None:
        _positive_int(self.entry_id, field_name="entry_id")
        _positive_int(self.entry_number, field_name="entry_number")


@dataclass(frozen=True, slots=True)
class MatchOddsRefreshTargetMatch:
    """Current locked Match and active-pool authority."""

    match_id: int
    match_name: str
    source_kind: MatchSourceKind
    status: MatchStatus
    grade: MatchGrade
    scheduled_at: datetime
    entries: tuple[MatchOddsRefreshEntry, ...] = field(default_factory=tuple)
    active_bets: tuple[BetPoolStake, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _text(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "source_kind", MatchSourceKind(self.source_kind))
        object.__setattr__(self, "status", MatchStatus(self.status))
        object.__setattr__(self, "grade", MatchGrade(self.grade))
        object.__setattr__(
            self,
            "scheduled_at",
            normalize_utc_datetime(self.scheduled_at, field_name="scheduled_at"),
        )
        entries = tuple(sorted(self.entries, key=lambda entry: entry.entry_number))
        if not entries or tuple(entry.entry_number for entry in entries) != tuple(range(1, len(entries) + 1)):
            raise ValueError("Open Match Entries must be non-empty and contiguous from 1.")
        if len({entry.entry_id for entry in entries}) != len(entries):
            raise ValueError("Open Match Entry IDs must be unique.")
        object.__setattr__(self, "entries", entries)
        active_bets = tuple(self.active_bets)
        if any(not isinstance(bet, BetPoolStake) for bet in active_bets):
            raise ValueError("active_bets must contain BetPoolStake values.")
        object.__setattr__(self, "active_bets", active_bets)
        if self.source_kind is not MatchSourceKind.NATIVE_V2 or self.status is not MatchStatus.BETTING_OPEN:
            raise ValueError("Periodic odds target must be a native betting-open Match.")

    def to_publication_match(self) -> MatchOddsRefreshMatch:
        number_by_id = {entry.entry_id: entry.entry_number for entry in self.entries}
        odds = calculate_provisional_odds(tuple(number_by_id), self.active_bets)
        return MatchOddsRefreshMatch(
            match_id=self.match_id,
            match_name=self.match_name,
            grade=self.grade,
            scheduled_at=self.scheduled_at,
            selections=tuple(
                MatchOddsRefreshSelection(
                    bet_type=item.bet_type,
                    entry_numbers=tuple(number_by_id[entry_id] for entry_id in item.selection_ids),
                    provisional_odds=item.odds,
                )
                for item in odds
            ),
        )


@dataclass(frozen=True, slots=True)
class MatchOddsRefreshTarget:
    """One fresh locked configured-guild cursor plus every open Match."""

    cursor: MatchOddsRefreshCursor
    matches: tuple[MatchOddsRefreshTargetMatch, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.cursor, MatchOddsRefreshCursor):
            raise ValueError("cursor must be MatchOddsRefreshCursor.")
        matches = tuple(sorted(self.matches, key=lambda item: (item.scheduled_at, item.match_id)))
        if len({match.match_id for match in matches}) != len(matches):
            raise ValueError("Periodic odds target contains duplicate Matches.")
        object.__setattr__(self, "matches", matches)


@dataclass(frozen=True, slots=True)
class StoredMatchOddsRefreshPublication:
    """Inserted durable refresh identity returned inside the UoW."""

    publication_id: int
    event_key: str
    payload_fingerprint: str
    status: PublicationStatus
    target_channel_id: str | None

    def __post_init__(self) -> None:
        _positive_int(self.publication_id, field_name="publication_id")
        object.__setattr__(self, "event_key", _text(self.event_key, field_name="event_key", max_length=128))
        object.__setattr__(
            self,
            "payload_fingerprint",
            _sha256(self.payload_fingerprint, field_name="payload_fingerprint"),
        )
        object.__setattr__(self, "status", PublicationStatus(self.status))
        object.__setattr__(
            self,
            "target_channel_id",
            _text(self.target_channel_id, field_name="target_channel_id", max_length=32, optional=True),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "publication_id": self.publication_id,
            "event_key": self.event_key,
            "payload_fingerprint": self.payload_fingerprint,
            "status": self.status.value,
            "target_channel_id": self.target_channel_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> StoredMatchOddsRefreshPublication:
        expected = {"publication_id", "event_key", "payload_fingerprint", "status", "target_channel_id"}
        if set(payload) != expected:
            raise ValueError("Stored Match odds publication keys are malformed.")
        return cls(
            publication_id=payload["publication_id"],  # type: ignore[arg-type]
            event_key=payload["event_key"],  # type: ignore[arg-type]
            payload_fingerprint=payload["payload_fingerprint"],  # type: ignore[arg-type]
            status=payload["status"],  # type: ignore[arg-type]
            target_channel_id=payload["target_channel_id"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class MatchOddsRefreshRun:
    """One periodic due-check result for runtime observability."""

    guild_id: str
    mode: MatchOddsRefreshMode
    open_match_count: int
    next_refresh_at: datetime | None
    publication: StoredMatchOddsRefreshPublication | None
    cursor_changed: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "guild_id", _text(self.guild_id, field_name="guild_id", max_length=32))
        object.__setattr__(self, "mode", MatchOddsRefreshMode(self.mode))
        _non_negative_int(self.open_match_count, field_name="open_match_count")
        if self.next_refresh_at is not None:
            object.__setattr__(
                self,
                "next_refresh_at",
                normalize_utc_datetime(self.next_refresh_at, field_name="next_refresh_at"),
            )
        if self.publication is not None and not isinstance(self.publication, StoredMatchOddsRefreshPublication):
            raise ValueError("publication must be StoredMatchOddsRefreshPublication or null.")
        if not isinstance(self.cursor_changed, bool):
            raise ValueError("cursor_changed must be a boolean.")


@dataclass(frozen=True, slots=True)
class ChangeMatchOddsRefreshMode:
    """Final staff interaction changing one configured-guild coverage mode."""

    guild_id: str
    desired_mode: MatchOddsRefreshMode
    actor_discord_user_id: str
    idempotency_key: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        for field_name, max_length, optional in (
            ("guild_id", 32, False),
            ("actor_discord_user_id", 32, False),
            ("idempotency_key", 128, False),
            ("correlation_id", 128, True),
        ):
            object.__setattr__(
                self,
                field_name,
                _text(getattr(self, field_name), field_name=field_name, max_length=max_length, optional=optional),
            )
        object.__setattr__(self, "desired_mode", MatchOddsRefreshMode(self.desired_mode))

    @property
    def request_fingerprint(self) -> str:
        return _fingerprint(
            {
                "schema": "match-odds-refresh-mode-command-v1",
                "guild_id": self.guild_id,
                "desired_mode": self.desired_mode.value,
            }
        )


@dataclass(frozen=True, slots=True)
class ChangedMatchOddsRefreshMode:
    """Committed staff mode-change receipt and exact-retry evidence."""

    guild_id: str
    previous_mode: MatchOddsRefreshMode
    mode: MatchOddsRefreshMode
    open_match_count: int
    next_refresh_at: datetime
    changed_at: datetime
    publication: StoredMatchOddsRefreshPublication | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "guild_id", _text(self.guild_id, field_name="guild_id", max_length=32))
        object.__setattr__(self, "previous_mode", MatchOddsRefreshMode(self.previous_mode))
        object.__setattr__(self, "mode", MatchOddsRefreshMode(self.mode))
        if self.previous_mode is self.mode:
            raise ValueError("A mode-change receipt must change mode.")
        _positive_int(self.open_match_count, field_name="open_match_count")
        object.__setattr__(
            self,
            "next_refresh_at",
            normalize_utc_datetime(self.next_refresh_at, field_name="next_refresh_at"),
        )
        object.__setattr__(
            self,
            "changed_at",
            normalize_utc_datetime(self.changed_at, field_name="changed_at"),
        )
        if self.publication is not None and not isinstance(self.publication, StoredMatchOddsRefreshPublication):
            raise ValueError("publication must be StoredMatchOddsRefreshPublication or null.")
        if self.mode is MatchOddsRefreshMode.LIVE and self.publication is None:
            raise ValueError("NORMAL to LIVE must retain its immediate publication receipt.")
        if self.mode is MatchOddsRefreshMode.NORMAL and self.publication is not None:
            raise ValueError("LIVE to NORMAL must not create an immediate publication.")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": MATCH_ODDS_MODE_AUDIT_SCHEMA_VERSION,
            "guild_id": self.guild_id,
            "previous_mode": self.previous_mode.value,
            "mode": self.mode.value,
            "open_match_count": self.open_match_count,
            "next_refresh_at": self.next_refresh_at.isoformat(),
            "changed_at": self.changed_at.isoformat(),
            "publication": self.publication.to_payload() if self.publication is not None else None,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> ChangedMatchOddsRefreshMode:
        expected = {
            "schema_version",
            "guild_id",
            "previous_mode",
            "mode",
            "open_match_count",
            "next_refresh_at",
            "changed_at",
            "publication",
        }
        if set(payload) != expected or payload.get("schema_version") != MATCH_ODDS_MODE_AUDIT_SCHEMA_VERSION:
            raise ValueError("Stored Match odds mode receipt keys are malformed.")
        raw_publication = payload["publication"]
        if raw_publication is not None and not isinstance(raw_publication, Mapping):
            raise ValueError("Stored Match odds publication receipt is malformed.")
        return cls(
            guild_id=payload["guild_id"],  # type: ignore[arg-type]
            previous_mode=payload["previous_mode"],  # type: ignore[arg-type]
            mode=payload["mode"],  # type: ignore[arg-type]
            open_match_count=payload["open_match_count"],  # type: ignore[arg-type]
            next_refresh_at=datetime.fromisoformat(payload["next_refresh_at"]),  # type: ignore[arg-type]
            changed_at=datetime.fromisoformat(payload["changed_at"]),  # type: ignore[arg-type]
            publication=(
                StoredMatchOddsRefreshPublication.from_payload(raw_publication) if raw_publication is not None else None
            ),
        )


@dataclass(frozen=True, slots=True)
class StoredMatchOddsModeOperation:
    request_fingerprint: str | None
    type: str | None
    guild_id: str | None
    after_data: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class MatchOddsRefreshStatus:
    """Detached read-only status for the private staff View."""

    guild_id: str
    mode: MatchOddsRefreshMode
    next_refresh_at: datetime | None
    open_match_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "guild_id", _text(self.guild_id, field_name="guild_id", max_length=32))
        object.__setattr__(self, "mode", MatchOddsRefreshMode(self.mode))
        if self.next_refresh_at is not None:
            object.__setattr__(
                self,
                "next_refresh_at",
                normalize_utc_datetime(self.next_refresh_at, field_name="next_refresh_at"),
            )
        _non_negative_int(self.open_match_count, field_name="open_match_count")


class MatchOddsPublicationRepository(Protocol):
    def lock_target(self, *, guild_id: str) -> MatchOddsRefreshTarget | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredMatchOddsModeOperation | None: ...

    def update_cursor(self, *, cursor: MatchOddsRefreshCursor, changed_at: datetime) -> None: ...

    def add_publication(
        self,
        *,
        intent: PublicationIntent,
        created_at: datetime,
    ) -> StoredMatchOddsRefreshPublication: ...

    def add_mode_audit(
        self,
        *,
        command: ChangeMatchOddsRefreshMode,
        before: MatchOddsRefreshCursor,
        after: ChangedMatchOddsRefreshMode,
        created_at: datetime,
    ) -> None: ...


class MatchOddsPublicationQueryRepository(Protocol):
    def get_status(self, *, guild_id: str) -> MatchOddsRefreshStatus | None: ...


class MatchOddsPublicationUnitOfWork(UnitOfWork, Protocol):
    @property
    def match_odds_publication(self) -> MatchOddsPublicationRepository: ...


class MatchOddsPublicationQueryUnitOfWork(UnitOfWork, Protocol):
    @property
    def match_odds_publication_queries(self) -> MatchOddsPublicationQueryRepository: ...


@dataclass(frozen=True, slots=True)
class MatchOddsPublicationCommands:
    """Application entry point for due refresh and staff mode changes."""

    command_runner: CommandRunner[MatchOddsPublicationUnitOfWork]
    clock: Callable[[], datetime]

    def refresh_due(self, *, guild_id: str) -> MatchOddsRefreshRun:
        normalized_guild = _text(guild_id, field_name="guild_id", max_length=32)
        assert normalized_guild is not None
        refreshed_at = self._now()
        return self.command_runner.run(
            lambda unit_of_work: self._refresh_due(
                unit_of_work.match_odds_publication,
                guild_id=normalized_guild,
                refreshed_at=refreshed_at,
            )
        )

    def change_mode(self, command: ChangeMatchOddsRefreshMode) -> ChangedMatchOddsRefreshMode:
        changed_at = self._now()
        return self.command_runner.run(
            lambda unit_of_work: self._change_mode(
                unit_of_work.match_odds_publication,
                command=command,
                changed_at=changed_at,
            )
        )

    def _refresh_due(
        self,
        repository: MatchOddsPublicationRepository,
        *,
        guild_id: str,
        refreshed_at: datetime,
    ) -> MatchOddsRefreshRun:
        target = self._lock_target(repository, guild_id=guild_id)
        cursor = target.cursor
        if not target.matches:
            reset = MatchOddsRefreshCursor(
                guild_id=cursor.guild_id,
                mode=MatchOddsRefreshMode.NORMAL,
                next_refresh_at=None,
                sequence=cursor.sequence,
                last_projection_fingerprint=None,
                destination=cursor.destination,
            )
            changed = reset != cursor
            if changed:
                repository.update_cursor(cursor=reset, changed_at=refreshed_at)
            return MatchOddsRefreshRun(
                guild_id=guild_id,
                mode=reset.mode,
                open_match_count=0,
                next_refresh_at=None,
                publication=None,
                cursor_changed=changed,
            )

        if cursor.next_refresh_at is None:
            started = MatchOddsRefreshCursor(
                guild_id=cursor.guild_id,
                mode=MatchOddsRefreshMode.NORMAL,
                next_refresh_at=refreshed_at + MatchOddsRefreshMode.NORMAL.interval,
                sequence=cursor.sequence,
                last_projection_fingerprint=None,
                destination=cursor.destination,
            )
            repository.update_cursor(cursor=started, changed_at=refreshed_at)
            return MatchOddsRefreshRun(
                guild_id=guild_id,
                mode=started.mode,
                open_match_count=len(target.matches),
                next_refresh_at=started.next_refresh_at,
                publication=None,
                cursor_changed=True,
            )

        if refreshed_at < cursor.next_refresh_at:
            return MatchOddsRefreshRun(
                guild_id=guild_id,
                mode=cursor.mode,
                open_match_count=len(target.matches),
                next_refresh_at=cursor.next_refresh_at,
                publication=None,
                cursor_changed=False,
            )

        source = self._build_source(target, mode=cursor.mode, sequence=cursor.sequence + 1, at=refreshed_at)
        next_refresh_at = refreshed_at + cursor.mode.interval
        if source.projection_fingerprint == cursor.last_projection_fingerprint:
            advanced = MatchOddsRefreshCursor(
                guild_id=cursor.guild_id,
                mode=cursor.mode,
                next_refresh_at=next_refresh_at,
                sequence=cursor.sequence,
                last_projection_fingerprint=cursor.last_projection_fingerprint,
                destination=cursor.destination,
            )
            repository.update_cursor(cursor=advanced, changed_at=refreshed_at)
            return MatchOddsRefreshRun(
                guild_id=guild_id,
                mode=advanced.mode,
                open_match_count=len(target.matches),
                next_refresh_at=next_refresh_at,
                publication=None,
                cursor_changed=True,
            )

        intent = build_match_odds_refresh_publication_intent(source)
        publication = repository.add_publication(intent=intent, created_at=refreshed_at)
        advanced = MatchOddsRefreshCursor(
            guild_id=cursor.guild_id,
            mode=cursor.mode,
            next_refresh_at=next_refresh_at,
            sequence=source.sequence,
            last_projection_fingerprint=source.projection_fingerprint,
            destination=cursor.destination,
        )
        repository.update_cursor(cursor=advanced, changed_at=refreshed_at)
        return MatchOddsRefreshRun(
            guild_id=guild_id,
            mode=advanced.mode,
            open_match_count=len(target.matches),
            next_refresh_at=next_refresh_at,
            publication=publication,
            cursor_changed=True,
        )

    def _change_mode(
        self,
        repository: MatchOddsPublicationRepository,
        *,
        command: ChangeMatchOddsRefreshMode,
        changed_at: datetime,
    ) -> ChangedMatchOddsRefreshMode:
        target = self._lock_target(repository, guild_id=command.guild_id)
        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)
        if not target.matches:
            raise MatchOddsPublicationUnavailableError("Periodic odds mode requires an open Match.")
        cursor = target.cursor
        if cursor.mode is command.desired_mode:
            raise MatchOddsPublicationNoChangeError("Periodic odds mode is already selected.")

        publication: StoredMatchOddsRefreshPublication | None = None
        sequence = cursor.sequence
        last_fingerprint = cursor.last_projection_fingerprint
        if command.desired_mode is MatchOddsRefreshMode.LIVE:
            source = self._build_source(
                target,
                mode=command.desired_mode,
                sequence=cursor.sequence + 1,
                at=changed_at,
            )
            publication = repository.add_publication(
                intent=build_match_odds_refresh_publication_intent(source),
                created_at=changed_at,
            )
            sequence = source.sequence
            last_fingerprint = source.projection_fingerprint

        next_refresh_at = changed_at + command.desired_mode.interval
        updated = MatchOddsRefreshCursor(
            guild_id=cursor.guild_id,
            mode=command.desired_mode,
            next_refresh_at=next_refresh_at,
            sequence=sequence,
            last_projection_fingerprint=last_fingerprint,
            destination=cursor.destination,
        )
        repository.update_cursor(cursor=updated, changed_at=changed_at)
        after = ChangedMatchOddsRefreshMode(
            guild_id=cursor.guild_id,
            previous_mode=cursor.mode,
            mode=updated.mode,
            open_match_count=len(target.matches),
            next_refresh_at=next_refresh_at,
            changed_at=changed_at,
            publication=publication,
        )
        repository.add_mode_audit(
            command=command,
            before=cursor,
            after=after,
            created_at=changed_at,
        )
        return after

    @staticmethod
    def _lock_target(
        repository: MatchOddsPublicationRepository,
        *,
        guild_id: str,
    ) -> MatchOddsRefreshTarget:
        try:
            target = repository.lock_target(guild_id=guild_id)
        except (KeyError, TypeError, ValueError) as exc:
            raise MatchOddsPublicationInvalidSourceError("Stored periodic odds authority is malformed.") from exc
        if target is None:
            raise MatchOddsPublicationUnavailableError("Discord guild settings are unavailable.")
        return target

    @staticmethod
    def _build_source(
        target: MatchOddsRefreshTarget,
        *,
        mode: MatchOddsRefreshMode,
        sequence: int,
        at: datetime,
    ) -> MatchOddsRefreshPublicationSource:
        try:
            return MatchOddsRefreshPublicationSource(
                destination=target.cursor.destination,
                mode=mode,
                sequence=sequence,
                generated_at=at,
                matches=tuple(match.to_publication_match() for match in target.matches),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MatchOddsPublicationInvalidSourceError(
                "Current Match pool did not produce a complete odds projection."
            ) from exc

    @staticmethod
    def _resolve_exact_retry(
        *,
        stored: StoredMatchOddsModeOperation,
        command: ChangeMatchOddsRefreshMode,
    ) -> ChangedMatchOddsRefreshMode:
        if (
            stored.type != MATCH_ODDS_MODE_OPERATION_TYPE
            or stored.request_fingerprint != command.request_fingerprint
            or stored.guild_id != command.guild_id
        ):
            raise MatchOddsPublicationIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise MatchOddsPublicationAuditError("Exact-retry operation has no stored after snapshot.")
        try:
            result = ChangedMatchOddsRefreshMode.from_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise MatchOddsPublicationAuditError("Exact-retry operation has malformed evidence.") from exc
        if result.guild_id != command.guild_id or result.mode is not command.desired_mode:
            raise MatchOddsPublicationAuditError("Exact-retry operation belongs to another mode change.")
        return result

    def _now(self) -> datetime:
        return normalize_utc_datetime(self.clock(), field_name="clock result")


@dataclass(frozen=True, slots=True)
class MatchOddsPublicationQueries:
    query_runner: QueryRunner[MatchOddsPublicationQueryUnitOfWork]

    def get_status(self, *, guild_id: str) -> MatchOddsRefreshStatus:
        normalized = _text(guild_id, field_name="guild_id", max_length=32)
        assert normalized is not None
        status = self.query_runner.run(
            lambda unit_of_work: unit_of_work.match_odds_publication_queries.get_status(guild_id=normalized)
        )
        if status is None:
            raise MatchOddsPublicationUnavailableError("Discord guild settings are unavailable.")
        return status
