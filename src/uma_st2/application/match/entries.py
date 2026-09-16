"""Native V2 Circle Match complete Entry-roster replacement boundary."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, UnitOfWork
from uma_st2.domain.identity import GameRegion
from uma_st2.domain.match import MATCH_ENTRY_MAXIMUM_COUNT, MatchSourceKind, MatchStatus
from uma_st2.shared import normalize_utc_datetime

MATCH_ENTRY_AUDIT_SCHEMA_VERSION: Final = 1
_EDITABLE_STATUSES: Final = frozenset({MatchStatus.SCHEDULED, MatchStatus.ENTRY_CONFIRMED})


class MatchEntryAuditType(StrEnum):
    """Canonical operation type for complete Match Entry replacement."""

    REPLACED = "match_entries_replaced"


class MatchEntryCommandError(ValueError):
    """Base error for rejected Match Entry commands."""


class MatchEntryUnavailableError(MatchEntryCommandError):
    """The target Match does not allow native pre-open roster mutation."""


class MatchEntryInvalidSourceError(MatchEntryCommandError):
    """Persisted or referenced Entry facts are malformed."""


class MatchEntryStaleError(MatchEntryCommandError):
    """The current roster changed after the operator preview."""


class MatchEntryNoChangeError(MatchEntryCommandError):
    """The desired roster is identical to the current canonical roster."""


class MatchEntryIdempotencyConflictError(MatchEntryCommandError):
    """An idempotency key is bound to another logical operation."""


class MatchEntryAuditError(MatchEntryCommandError):
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
class MatchEntryReference:
    """One desired canonical Entry reference; raw PID never crosses this boundary."""

    entry_number: int
    game_account_id: int
    umamusume_id: int
    umamusume_variant_id: int | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.entry_number, field_name="entry_number")
        _require_positive_int(self.game_account_id, field_name="game_account_id")
        _require_positive_int(self.umamusume_id, field_name="umamusume_id")
        if self.umamusume_variant_id is not None:
            _require_positive_int(self.umamusume_variant_id, field_name="umamusume_variant_id")


@dataclass(frozen=True, slots=True)
class MatchEntryAccountTarget:
    """Locked current GameAccount facts used for one new event snapshot."""

    id: int
    persona_id: str
    game_region: GameRegion
    nickname: str
    affiliation: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        object.__setattr__(
            self, "persona_id", _normalized_string(self.persona_id, field_name="persona_id", max_length=36)
        )
        object.__setattr__(self, "game_region", GameRegion(self.game_region))
        object.__setattr__(self, "nickname", _normalized_string(self.nickname, field_name="nickname", max_length=100))
        object.__setattr__(
            self,
            "affiliation",
            _normalized_string(self.affiliation, field_name="affiliation", max_length=100, optional=True),
        )


@dataclass(frozen=True, slots=True)
class MatchEntryCharacterTarget:
    """Locked current base/optional variant master relationship."""

    umamusume_id: int
    umamusume_variant_id: int | None
    display_name: str

    def __post_init__(self) -> None:
        _require_positive_int(self.umamusume_id, field_name="umamusume_id")
        if self.umamusume_variant_id is not None:
            _require_positive_int(self.umamusume_variant_id, field_name="umamusume_variant_id")
        object.__setattr__(
            self,
            "display_name",
            _normalized_string(self.display_name, field_name="display_name", max_length=100),
        )

    @property
    def identity(self) -> tuple[int, int | None]:
        return (self.umamusume_id, self.umamusume_variant_id)


@dataclass(frozen=True, slots=True)
class MatchEntrySnapshot:
    """One committed Entry plus safe display snapshots retained by operation audit."""

    entry_id: int
    entry_number: int
    game_account_id: int
    owner_at_event_persona_id: str
    affiliation_at_event: str | None
    game_region: GameRegion
    game_account_name: str
    umamusume_id: int
    umamusume_variant_id: int | None
    umamusume_name: str
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("entry_id", "entry_number", "game_account_id", "umamusume_id"):
            _require_positive_int(getattr(self, field_name), field_name=field_name)
        if self.umamusume_variant_id is not None:
            _require_positive_int(self.umamusume_variant_id, field_name="umamusume_variant_id")
        object.__setattr__(
            self,
            "owner_at_event_persona_id",
            _normalized_string(
                self.owner_at_event_persona_id,
                field_name="owner_at_event_persona_id",
                max_length=36,
            ),
        )
        object.__setattr__(
            self,
            "affiliation_at_event",
            _normalized_string(
                self.affiliation_at_event,
                field_name="affiliation_at_event",
                max_length=100,
                optional=True,
            ),
        )
        object.__setattr__(self, "game_region", GameRegion(self.game_region))
        object.__setattr__(
            self,
            "game_account_name",
            _normalized_string(self.game_account_name, field_name="game_account_name", max_length=100),
        )
        object.__setattr__(
            self,
            "umamusume_name",
            _normalized_string(self.umamusume_name, field_name="umamusume_name", max_length=100),
        )
        object.__setattr__(
            self,
            "created_at",
            normalize_utc_datetime(self.created_at, field_name="created_at"),
        )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "entry_id": self.entry_id,
            "entry_number": self.entry_number,
            "game_account_id": self.game_account_id,
            "owner_at_event_persona_id": self.owner_at_event_persona_id,
            "affiliation_at_event": self.affiliation_at_event,
            "game_region": self.game_region.value,
            "game_account_name": self.game_account_name,
            "umamusume_id": self.umamusume_id,
            "umamusume_variant_id": self.umamusume_variant_id,
            "umamusume_name": self.umamusume_name,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> MatchEntrySnapshot:
        variant_id = payload["umamusume_variant_id"]
        if variant_id is not None:
            _require_positive_int(variant_id, field_name="umamusume_variant_id")  # type: ignore[arg-type]
        return cls(
            entry_id=_payload_positive_int(payload, "entry_id"),
            entry_number=_payload_positive_int(payload, "entry_number"),
            game_account_id=_payload_positive_int(payload, "game_account_id"),
            owner_at_event_persona_id=_payload_string(payload, "owner_at_event_persona_id"),
            affiliation_at_event=_payload_optional_string(payload, "affiliation_at_event"),
            game_region=GameRegion(_payload_string(payload, "game_region")),
            game_account_name=_payload_string(payload, "game_account_name"),
            umamusume_id=_payload_positive_int(payload, "umamusume_id"),
            umamusume_variant_id=variant_id,  # type: ignore[arg-type]
            umamusume_name=_payload_string(payload, "umamusume_name"),
            created_at=datetime.fromisoformat(_payload_string(payload, "created_at")),
        )


def fingerprint_match_entry_roster(entries: Sequence[MatchEntrySnapshot]) -> str:
    """Return the exact stale token for one ordered current roster."""

    canonical = [
        {
            "entry_id": entry.entry_id,
            "entry_number": entry.entry_number,
            "game_account_id": entry.game_account_id,
            "owner_at_event_persona_id": entry.owner_at_event_persona_id,
            "affiliation_at_event": entry.affiliation_at_event,
            "game_region": entry.game_region.value,
            "umamusume_id": entry.umamusume_id,
            "umamusume_variant_id": entry.umamusume_variant_id,
        }
        for entry in sorted(entries, key=lambda item: item.entry_number)
    ]
    encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MatchEntryRosterSnapshot:
    """Complete current or committed roster snapshot."""

    match_id: int
    match_name: str
    source_kind: MatchSourceKind
    status: MatchStatus
    entries: tuple[MatchEntrySnapshot, ...] = field(default_factory=tuple)
    roster_fingerprint: str = ""

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "source_kind", MatchSourceKind(self.source_kind))
        object.__setattr__(self, "status", MatchStatus(self.status))
        ordered = tuple(sorted(self.entries, key=lambda entry: entry.entry_number))
        expected_numbers = tuple(range(1, len(ordered) + 1))
        if tuple(entry.entry_number for entry in ordered) != expected_numbers:
            raise ValueError("Match Entry numbers must be contiguous from 1.")
        if len({entry.entry_id for entry in ordered}) != len(ordered):
            raise ValueError("Match Entry IDs must be unique.")
        if len({entry.game_region for entry in ordered}) > 1:
            raise ValueError("All Match Entries must share one game region.")
        object.__setattr__(self, "entries", ordered)
        calculated = fingerprint_match_entry_roster(ordered)
        if self.roster_fingerprint and self.roster_fingerprint != calculated:
            raise ValueError("roster_fingerprint does not match entries.")
        object.__setattr__(self, "roster_fingerprint", calculated)

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": MATCH_ENTRY_AUDIT_SCHEMA_VERSION,
            "match_id": self.match_id,
            "match_name": self.match_name,
            "source_kind": self.source_kind.value,
            "status": self.status.value,
            "roster_fingerprint": self.roster_fingerprint,
            "entries": [entry.to_audit_payload() for entry in self.entries],
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> MatchEntryRosterSnapshot:
        if payload.get("schema_version") != MATCH_ENTRY_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported Match Entry audit schema version.")
        raw_entries = payload["entries"]
        if not isinstance(raw_entries, list) or not all(isinstance(item, Mapping) for item in raw_entries):
            raise ValueError("entries must be a list of objects.")
        return cls(
            match_id=_payload_positive_int(payload, "match_id"),
            match_name=_payload_string(payload, "match_name"),
            source_kind=MatchSourceKind(_payload_string(payload, "source_kind")),
            status=MatchStatus(_payload_string(payload, "status")),
            entries=tuple(MatchEntrySnapshot.from_audit_payload(item) for item in raw_entries),
            roster_fingerprint=_payload_string(payload, "roster_fingerprint"),
        )


@dataclass(frozen=True, slots=True)
class ReplaceMatchEntries:
    """Replace one complete previewed native Match roster."""

    match_id: int
    entries: tuple[MatchEntryReference, ...]
    expected_roster_fingerprint: str
    idempotency_key: str
    actor_discord_user_id: str
    guild_id: str | None = None
    correlation_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        ordered = tuple(sorted(self.entries, key=lambda entry: entry.entry_number))
        if not ordered:
            raise ValueError("entries must contain at least one Match Entry.")
        if len(ordered) > MATCH_ENTRY_MAXIMUM_COUNT:
            raise ValueError(f"entries must contain at most {MATCH_ENTRY_MAXIMUM_COUNT} Match Entries.")
        if tuple(entry.entry_number for entry in ordered) != tuple(range(1, len(ordered) + 1)):
            raise ValueError("entries must use contiguous entry_number values from 1.")
        object.__setattr__(self, "entries", ordered)
        if len(self.expected_roster_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.expected_roster_fingerprint
        ):
            raise ValueError("expected_roster_fingerprint must be a lowercase SHA-256 digest.")
        for field_name, max_length, optional in (
            ("idempotency_key", 128, False),
            ("actor_discord_user_id", 32, False),
            ("guild_id", 32, True),
            ("correlation_id", 128, True),
            ("reason", 255, True),
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
        canonical = {
            "schema": "match-entry-replacement-command-v1",
            "match_id": self.match_id,
            "expected_roster_fingerprint": self.expected_roster_fingerprint,
            "entries": [
                {
                    "entry_number": entry.entry_number,
                    "game_account_id": entry.game_account_id,
                    "umamusume_id": entry.umamusume_id,
                    "umamusume_variant_id": entry.umamusume_variant_id,
                }
                for entry in self.entries
            ],
        }
        encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReplacedMatchEntries:
    """Committed complete roster returned by save or exact retry."""

    snapshot: MatchEntryRosterSnapshot
    operation_type: MatchEntryAuditType = MatchEntryAuditType.REPLACED

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, MatchEntryRosterSnapshot):
            raise ValueError("snapshot must be MatchEntryRosterSnapshot.")
        if not self.snapshot.entries:
            raise ValueError("A committed Match roster must contain at least one Entry.")
        object.__setattr__(self, "operation_type", MatchEntryAuditType(self.operation_type))


@dataclass(frozen=True, slots=True)
class StoredMatchEntryOperation:
    """Minimal stored operation data used for exact retry."""

    request_fingerprint: str | None
    type: str | None
    match_id: int | None
    after_data: Mapping[str, object] | None


class MatchEntryRepository(Protocol):
    """Persistence operations required by complete roster replacement."""

    def lock_match(self, *, match_id: int) -> MatchEntryRosterSnapshot | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredMatchEntryOperation | None: ...

    def lock_current_entries(self, *, match_id: int) -> tuple[MatchEntrySnapshot, ...]: ...

    def has_roster_dependents(self, *, match_id: int, entry_ids: tuple[int, ...]) -> bool: ...

    def lock_game_accounts(self, *, account_ids: tuple[int, ...]) -> tuple[MatchEntryAccountTarget, ...]: ...

    def lock_characters(
        self,
        *,
        identities: tuple[tuple[int, int | None], ...],
    ) -> tuple[MatchEntryCharacterTarget, ...]: ...

    def replace_entries(
        self,
        *,
        match_id: int,
        entries: tuple[MatchEntryReference, ...],
        accounts: Mapping[int, MatchEntryAccountTarget],
        characters: Mapping[tuple[int, int | None], MatchEntryCharacterTarget],
        changed_at: datetime,
    ) -> tuple[MatchEntrySnapshot, ...]: ...

    def add_audit(
        self,
        *,
        command: ReplaceMatchEntries,
        before: MatchEntryRosterSnapshot,
        after: MatchEntryRosterSnapshot,
        created_at: datetime,
    ) -> None: ...


class MatchEntryUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only Match Entry replacement persistence."""

    @property
    def match_entries(self) -> MatchEntryRepository: ...


@dataclass(frozen=True, slots=True)
class MatchEntryCommands:
    """Application entry point for atomic complete roster replacement."""

    command_runner: CommandRunner[MatchEntryUnitOfWork]
    clock: Callable[[], datetime]

    def replace_entries(self, command: ReplaceMatchEntries) -> ReplacedMatchEntries:
        return self.command_runner.run(lambda unit_of_work: self._replace(unit_of_work.match_entries, command))

    def _replace(
        self,
        repository: MatchEntryRepository,
        command: ReplaceMatchEntries,
    ) -> ReplacedMatchEntries:
        match = repository.lock_match(match_id=command.match_id)
        if match is None:
            raise MatchEntryUnavailableError("Match does not exist.")

        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)

        if match.source_kind != MatchSourceKind.NATIVE_V2 or match.status not in _EDITABLE_STATUSES:
            raise MatchEntryUnavailableError("Match Entry replacement requires a native pre-open Match.")

        try:
            current_entries = repository.lock_current_entries(match_id=match.match_id)
            current = MatchEntryRosterSnapshot(
                match_id=match.match_id,
                match_name=match.match_name,
                source_kind=match.source_kind,
                status=match.status,
                entries=current_entries,
            )
        except (TypeError, ValueError) as exc:
            raise MatchEntryInvalidSourceError("Stored Match Entry facts are malformed.") from exc
        if current.roster_fingerprint != command.expected_roster_fingerprint:
            raise MatchEntryStaleError("Match Entry roster changed after preview.")
        if repository.has_roster_dependents(
            match_id=match.match_id,
            entry_ids=tuple(entry.entry_id for entry in current.entries),
        ):
            raise MatchEntryInvalidSourceError("A pre-open Match contains roster-dependent facts.")

        account_ids = tuple(sorted({entry.game_account_id for entry in command.entries}))
        accounts = {account.id: account for account in repository.lock_game_accounts(account_ids=account_ids)}
        if set(accounts) != set(account_ids):
            raise MatchEntryInvalidSourceError("Every desired Entry must reference a current GameAccount.")
        if len({account.game_region for account in accounts.values()}) != 1:
            raise MatchEntryInvalidSourceError("All desired Match Entries must share one game region.")

        identities = tuple(
            sorted(
                {(entry.umamusume_id, entry.umamusume_variant_id) for entry in command.entries},
                key=lambda identity: (identity[0], identity[1] or 0),
            )
        )
        characters = {character.identity: character for character in repository.lock_characters(identities=identities)}
        if set(characters) != set(identities):
            raise MatchEntryInvalidSourceError("Every desired Entry must reference one exact current character.")

        current_semantics = tuple(
            (
                entry.entry_number,
                entry.game_account_id,
                entry.owner_at_event_persona_id,
                entry.affiliation_at_event,
                entry.umamusume_id,
                entry.umamusume_variant_id,
            )
            for entry in current.entries
        )
        desired_semantics = tuple(
            (
                entry.entry_number,
                entry.game_account_id,
                accounts[entry.game_account_id].persona_id,
                accounts[entry.game_account_id].affiliation,
                entry.umamusume_id,
                entry.umamusume_variant_id,
            )
            for entry in command.entries
        )
        if current_semantics == desired_semantics:
            raise MatchEntryNoChangeError("Desired Match Entry roster is identical to current state.")

        changed_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        try:
            persisted = repository.replace_entries(
                match_id=match.match_id,
                entries=command.entries,
                accounts=accounts,
                characters=characters,
                changed_at=changed_at,
            )
            after = MatchEntryRosterSnapshot(
                match_id=match.match_id,
                match_name=match.match_name,
                source_kind=match.source_kind,
                status=match.status,
                entries=persisted,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MatchEntryInvalidSourceError("Replacement did not produce one complete canonical roster.") from exc
        repository.add_audit(
            command=command,
            before=current,
            after=after,
            created_at=changed_at,
        )
        return ReplacedMatchEntries(snapshot=after)

    @staticmethod
    def _resolve_exact_retry(
        *,
        stored: StoredMatchEntryOperation,
        command: ReplaceMatchEntries,
    ) -> ReplacedMatchEntries:
        if (
            stored.type != MatchEntryAuditType.REPLACED.value
            or stored.request_fingerprint != command.request_fingerprint
            or stored.match_id != command.match_id
        ):
            raise MatchEntryIdempotencyConflictError("Idempotency key is already bound to another logical operation.")
        if stored.after_data is None:
            raise MatchEntryAuditError("Exact-retry operation has no stored after snapshot.")
        try:
            snapshot = MatchEntryRosterSnapshot.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise MatchEntryAuditError("Exact-retry operation has malformed stored evidence.") from exc
        if snapshot.match_id != command.match_id:
            raise MatchEntryAuditError("Exact-retry snapshot belongs to another Match.")
        return ReplacedMatchEntries(snapshot=snapshot)
