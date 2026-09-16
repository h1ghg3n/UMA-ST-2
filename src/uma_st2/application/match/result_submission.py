"""Native V2 Match ResultSubmission command boundary."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, UnitOfWork
from uma_st2.domain.match import (
    MatchResultSourceKind,
    MatchResultSubmissionStatus,
    MatchSourceKind,
    MatchStatus,
)
from uma_st2.shared import normalize_utc_datetime

MATCH_RESULT_CANDIDATE_SCHEMA_VERSION: Final = 1
MATCH_RESULT_SUBMISSION_AUDIT_SCHEMA_VERSION: Final = 1


class MatchResultSubmissionAuditType(StrEnum):
    """Canonical operation types for pending candidate creation."""

    SUBMITTED = "match_result_submitted"
    REVISED = "match_result_revised"


class MatchResultSubmissionError(ValueError):
    """Base error for rejected result candidate submission."""


class MatchResultSubmissionUnavailableError(MatchResultSubmissionError):
    """The Match is absent or not eligible for a pending result revision."""


class MatchResultSubmissionStaleError(MatchResultSubmissionError):
    """The previewed Match/Entry/revision state is no longer current."""


class MatchResultSubmissionReasonRequiredError(MatchResultSubmissionError):
    """A confirmed-result correction lacks its required reason."""


class MatchResultSubmissionIdempotencyConflictError(MatchResultSubmissionError):
    """An idempotency key is bound to another logical request."""


class MatchResultSubmissionInvalidSourceError(MatchResultSubmissionError):
    """Persisted Match, Entry, revision, or audit facts are malformed."""


class MatchResultSubmissionAuditError(MatchResultSubmissionError):
    """Stored exact-retry evidence is absent or malformed."""


def _require_positive_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")
    return value


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
    return _require_positive_int(payload[key], field_name=key)  # type: ignore[arg-type]


def _payload_optional_positive_int(payload: Mapping[str, object], key: str) -> int | None:
    value = payload.get(key)
    return None if value is None else _require_positive_int(value, field_name=key)  # type: ignore[arg-type]


def _payload_string(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string.")
    return value


@dataclass(frozen=True, slots=True)
class MatchResultCandidateEntry:
    """One normalized complete-board row in a pending candidate."""

    entry_id: int
    entry_number: int
    rank: int
    popularity_rank: int | None = None
    margin: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.entry_id, field_name="entry_id")
        _require_positive_int(self.entry_number, field_name="entry_number")
        _require_positive_int(self.rank, field_name="rank")
        if self.popularity_rank is not None:
            _require_positive_int(self.popularity_rank, field_name="popularity_rank")
        margin = _normalized_string(
            self.margin,
            field_name="margin",
            max_length=16,
            optional=True,
        )
        if self.rank == 1 and margin is not None:
            raise ValueError("The winner margin must be absent.")
        object.__setattr__(self, "margin", margin)

    def to_payload(self) -> dict[str, object]:
        return {
            "entry_id": self.entry_id,
            "entry_number": self.entry_number,
            "rank": self.rank,
            "popularity_rank": self.popularity_rank,
            "margin": self.margin,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> MatchResultCandidateEntry:
        margin = payload.get("margin")
        if margin is not None and not isinstance(margin, str):
            raise ValueError("margin must be a string or null.")
        return cls(
            entry_id=_payload_positive_int(payload, "entry_id"),
            entry_number=_payload_positive_int(payload, "entry_number"),
            rank=_payload_positive_int(payload, "rank"),
            popularity_rank=_payload_optional_positive_int(payload, "popularity_rank"),
            margin=margin,
        )


@dataclass(frozen=True, slots=True)
class MatchResultCandidate:
    """Provider-independent complete result candidate v1."""

    entries: tuple[MatchResultCandidateEntry, ...]
    finish_time_ms: int | None = None

    def __post_init__(self) -> None:
        entries = tuple(sorted(self.entries, key=lambda entry: entry.rank))
        if not entries or any(not isinstance(entry, MatchResultCandidateEntry) for entry in entries):
            raise ValueError("entries must contain a complete result board.")
        expected_ranks = tuple(range(1, len(entries) + 1))
        if tuple(entry.rank for entry in entries) != expected_ranks:
            raise ValueError("Candidate ranks must be unique and contiguous from 1 to field size.")
        if len({entry.entry_id for entry in entries}) != len(entries):
            raise ValueError("Candidate Entry IDs must be unique.")
        if len({entry.entry_number for entry in entries}) != len(entries):
            raise ValueError("Candidate Entry numbers must be unique.")
        popularity = tuple(entry.popularity_rank for entry in entries if entry.popularity_rank is not None)
        if len(set(popularity)) != len(popularity):
            raise ValueError("Non-null popularity ranks must be unique.")
        if any(value > len(entries) for value in popularity):
            raise ValueError("Popularity rank cannot exceed field size.")
        if self.finish_time_ms is not None:
            _require_positive_int(self.finish_time_ms, field_name="finish_time_ms")
            if self.finish_time_ms % 100 != 0:
                raise ValueError("finish_time_ms must preserve exact 0.1-second source precision.")
        object.__setattr__(self, "entries", entries)

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.to_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": MATCH_RESULT_CANDIDATE_SCHEMA_VERSION,
            "finish_time_ms": self.finish_time_ms,
            "entries": [entry.to_payload() for entry in self.entries],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> MatchResultCandidate:
        if payload.get("schema_version") != MATCH_RESULT_CANDIDATE_SCHEMA_VERSION:
            raise ValueError("Unsupported Match result candidate schema version.")
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, Sequence) or isinstance(raw_entries, (str, bytes)):
            raise ValueError("entries must be a sequence.")
        entries: list[MatchResultCandidateEntry] = []
        for value in raw_entries:
            if not isinstance(value, Mapping):
                raise ValueError("Each candidate Entry must be an object.")
            entries.append(MatchResultCandidateEntry.from_payload(value))
        return cls(
            entries=tuple(entries),
            finish_time_ms=_payload_optional_positive_int(payload, "finish_time_ms"),
        )


@dataclass(frozen=True, slots=True)
class MatchResultSubmissionEntry:
    """One current immutable Entry identity and display snapshot."""

    entry_id: int
    entry_number: int
    game_account_name: str
    horse_name: str

    def __post_init__(self) -> None:
        _require_positive_int(self.entry_id, field_name="entry_id")
        _require_positive_int(self.entry_number, field_name="entry_number")
        object.__setattr__(
            self,
            "game_account_name",
            _normalized_string(self.game_account_name, field_name="game_account_name", max_length=100),
        )
        object.__setattr__(
            self,
            "horse_name",
            _normalized_string(self.horse_name, field_name="horse_name", max_length=100),
        )

    def state_payload(self) -> dict[str, int]:
        return {"entry_id": self.entry_id, "entry_number": self.entry_number}


@dataclass(frozen=True, slots=True)
class MatchResultRevisionReference:
    """Current pending or confirmed revision authority."""

    submission_id: int
    revision_number: int
    status: MatchResultSubmissionStatus
    source_kind: MatchResultSourceKind
    candidate: MatchResultCandidate

    def __post_init__(self) -> None:
        _require_positive_int(self.submission_id, field_name="submission_id")
        _require_positive_int(self.revision_number, field_name="revision_number")
        object.__setattr__(self, "status", MatchResultSubmissionStatus(self.status))
        object.__setattr__(self, "source_kind", MatchResultSourceKind(self.source_kind))
        if not isinstance(self.candidate, MatchResultCandidate):
            raise ValueError("candidate must be MatchResultCandidate.")

    def state_payload(self) -> dict[str, object]:
        return {
            "submission_id": self.submission_id,
            "revision_number": self.revision_number,
            "status": self.status.value,
            "source_kind": self.source_kind.value,
            "candidate_fingerprint": self.candidate.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class MatchResultSubmissionTarget:
    """Locked/read-only Match authority used by preview and final submission."""

    match_id: int
    match_name: str
    source_kind: MatchSourceKind
    status: MatchStatus
    entries: tuple[MatchResultSubmissionEntry, ...]
    next_revision_number: int
    pending: MatchResultRevisionReference | None = None
    confirmed: MatchResultRevisionReference | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "source_kind", MatchSourceKind(self.source_kind))
        object.__setattr__(self, "status", MatchStatus(self.status))
        entries = tuple(sorted(self.entries, key=lambda entry: entry.entry_number))
        if not entries or any(not isinstance(entry, MatchResultSubmissionEntry) for entry in entries):
            raise ValueError("entries must contain at least one current Match Entry.")
        if len({entry.entry_id for entry in entries}) != len(entries):
            raise ValueError("Target Entry IDs must be unique.")
        if len({entry.entry_number for entry in entries}) != len(entries):
            raise ValueError("Target Entry numbers must be unique.")
        object.__setattr__(self, "entries", entries)
        _require_positive_int(self.next_revision_number, field_name="next_revision_number")
        if self.pending is not None and self.pending.status != MatchResultSubmissionStatus.PENDING:
            raise ValueError("pending reference must have pending status.")
        if self.confirmed is not None and self.confirmed.status != MatchResultSubmissionStatus.CONFIRMED:
            raise ValueError("confirmed reference must have confirmed status.")
        revisions = tuple(
            reference.revision_number for reference in (self.pending, self.confirmed) if reference is not None
        )
        if revisions and self.next_revision_number <= max(revisions):
            raise ValueError("next_revision_number must follow current revisions.")

    @property
    def state_fingerprint(self) -> str:
        return _fingerprint(
            {
                "schema": "match-result-submission-target-v1",
                "match_id": self.match_id,
                "source_kind": self.source_kind.value,
                "status": self.status.value,
                "entries": [entry.state_payload() for entry in self.entries],
                "next_revision_number": self.next_revision_number,
                "pending": self.pending.state_payload() if self.pending is not None else None,
                "confirmed": self.confirmed.state_payload() if self.confirmed is not None else None,
            }
        )

    @property
    def preferred_draft_candidate(self) -> MatchResultCandidate | None:
        if self.pending is not None:
            return self.pending.candidate
        if self.confirmed is not None:
            return self.confirmed.candidate
        return None


@dataclass(frozen=True, slots=True)
class SaveMatchResultSubmission:
    """One final complete manual candidate submission request."""

    match_id: int
    expected_state_fingerprint: str
    candidate: MatchResultCandidate
    source_kind: MatchResultSourceKind
    actor_discord_user_id: str
    guild_id: str
    idempotency_key: str
    correlation_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "expected_state_fingerprint",
            _normalized_string(
                self.expected_state_fingerprint,
                field_name="expected_state_fingerprint",
                max_length=64,
            ),
        )
        if not isinstance(self.candidate, MatchResultCandidate):
            raise ValueError("candidate must be MatchResultCandidate.")
        object.__setattr__(self, "source_kind", MatchResultSourceKind(self.source_kind))
        for field_name, max_length, optional in (
            ("actor_discord_user_id", 32, False),
            ("guild_id", 32, False),
            ("idempotency_key", 128, False),
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
        return _fingerprint(
            {
                "schema": "match-result-submission-command-v1",
                "match_id": self.match_id,
                "expected_state_fingerprint": self.expected_state_fingerprint,
                "candidate_fingerprint": self.candidate.fingerprint,
                "source_kind": self.source_kind.value,
                "actor_discord_user_id": self.actor_discord_user_id,
                "guild_id": self.guild_id,
                "reason": self.reason,
            }
        )


@dataclass(frozen=True, slots=True)
class SavedMatchResultSubmission:
    """Committed pending revision receipt returned after save or exact retry."""

    submission_id: int
    match_id: int
    match_name: str
    revision_number: int
    source_kind: MatchResultSourceKind
    status: MatchResultSubmissionStatus
    candidate: MatchResultCandidate
    candidate_fingerprint: str
    superseded_submission_id: int | None
    corrects_confirmed: bool
    submitted_at: datetime

    def __post_init__(self) -> None:
        _require_positive_int(self.submission_id, field_name="submission_id")
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        _require_positive_int(self.revision_number, field_name="revision_number")
        object.__setattr__(self, "source_kind", MatchResultSourceKind(self.source_kind))
        object.__setattr__(self, "status", MatchResultSubmissionStatus(self.status))
        if self.status != MatchResultSubmissionStatus.PENDING:
            raise ValueError("A saved result submission receipt must be pending.")
        if not isinstance(self.candidate, MatchResultCandidate):
            raise ValueError("candidate must be MatchResultCandidate.")
        object.__setattr__(
            self,
            "candidate_fingerprint",
            _normalized_string(
                self.candidate_fingerprint,
                field_name="candidate_fingerprint",
                max_length=64,
            ),
        )
        if self.candidate_fingerprint != self.candidate.fingerprint:
            raise ValueError("candidate_fingerprint does not match candidate.")
        if self.superseded_submission_id is not None:
            _require_positive_int(self.superseded_submission_id, field_name="superseded_submission_id")
        if not isinstance(self.corrects_confirmed, bool):
            raise ValueError("corrects_confirmed must be a boolean.")
        object.__setattr__(
            self,
            "submitted_at",
            normalize_utc_datetime(self.submitted_at, field_name="submitted_at"),
        )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": MATCH_RESULT_SUBMISSION_AUDIT_SCHEMA_VERSION,
            "submission_id": self.submission_id,
            "match_id": self.match_id,
            "match_name": self.match_name,
            "revision_number": self.revision_number,
            "source_kind": self.source_kind.value,
            "status": self.status.value,
            "candidate": self.candidate.to_payload(),
            "candidate_fingerprint": self.candidate_fingerprint,
            "superseded_submission_id": self.superseded_submission_id,
            "corrects_confirmed": self.corrects_confirmed,
            "submitted_at": self.submitted_at.isoformat(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> SavedMatchResultSubmission:
        if payload.get("schema_version") != MATCH_RESULT_SUBMISSION_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported result submission audit schema version.")
        candidate_payload = payload.get("candidate")
        if not isinstance(candidate_payload, Mapping):
            raise ValueError("candidate must be an object.")
        superseded = payload.get("superseded_submission_id")
        corrects_confirmed = payload.get("corrects_confirmed")
        if not isinstance(corrects_confirmed, bool):
            raise ValueError("corrects_confirmed must be a boolean.")
        return cls(
            submission_id=_payload_positive_int(payload, "submission_id"),
            match_id=_payload_positive_int(payload, "match_id"),
            match_name=_payload_string(payload, "match_name"),
            revision_number=_payload_positive_int(payload, "revision_number"),
            source_kind=MatchResultSourceKind(_payload_string(payload, "source_kind")),
            status=MatchResultSubmissionStatus(_payload_string(payload, "status")),
            candidate=MatchResultCandidate.from_payload(candidate_payload),
            candidate_fingerprint=_payload_string(payload, "candidate_fingerprint"),
            superseded_submission_id=(
                None if superseded is None else _require_positive_int(superseded, field_name="superseded_submission_id")  # type: ignore[arg-type]
            ),
            corrects_confirmed=corrects_confirmed,
            submitted_at=datetime.fromisoformat(_payload_string(payload, "submitted_at")),
        )


@dataclass(frozen=True, slots=True)
class StoredMatchResultSubmissionOperation:
    """Minimal operation facts used to resolve an exact retry."""

    request_fingerprint: str | None
    type: str | None
    match_id: int | None
    after_data: Mapping[str, object] | None


class MatchResultSubmissionRepository(Protocol):
    """Persistence operations for one pending candidate revision."""

    def lock_target(self, *, match_id: int) -> MatchResultSubmissionTarget | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredMatchResultSubmissionOperation | None: ...

    def persist_submission(
        self,
        *,
        command: SaveMatchResultSubmission,
        target: MatchResultSubmissionTarget,
        operation_type: MatchResultSubmissionAuditType,
        submitted_at: datetime,
    ) -> SavedMatchResultSubmission: ...


class MatchResultSubmissionUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing manual result submission persistence."""

    @property
    def match_result_submission(self) -> MatchResultSubmissionRepository: ...


@dataclass(frozen=True, slots=True)
class MatchResultSubmissionCommands:
    """Application entry point for complete pending result candidates."""

    command_runner: CommandRunner[MatchResultSubmissionUnitOfWork]
    clock: Callable[[], datetime]

    def submit(self, command: SaveMatchResultSubmission) -> SavedMatchResultSubmission:
        return self.command_runner.run(lambda unit_of_work: self._submit(unit_of_work.match_result_submission, command))

    def _submit(
        self,
        repository: MatchResultSubmissionRepository,
        command: SaveMatchResultSubmission,
    ) -> SavedMatchResultSubmission:
        try:
            target = repository.lock_target(match_id=command.match_id)
        except (TypeError, ValueError) as exc:
            raise MatchResultSubmissionInvalidSourceError(
                "Stored Match result submission facts are malformed."
            ) from exc
        if target is None:
            raise MatchResultSubmissionUnavailableError("Match does not exist.")

        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)

        if target.source_kind != MatchSourceKind.NATIVE_V2 or target.status not in {
            MatchStatus.BETTING_CLOSED,
            MatchStatus.RESULT_CONFIRMED,
        }:
            raise MatchResultSubmissionUnavailableError(
                "Result submission requires a native betting-closed or result-confirmed Match."
            )
        if target.status == MatchStatus.BETTING_CLOSED and target.confirmed is not None:
            raise MatchResultSubmissionInvalidSourceError(
                "A betting-closed Match cannot have confirmed result authority."
            )
        if target.status == MatchStatus.RESULT_CONFIRMED and target.confirmed is None:
            raise MatchResultSubmissionInvalidSourceError(
                "A result-confirmed Match requires confirmed result authority."
            )
        if target.state_fingerprint != command.expected_state_fingerprint:
            raise MatchResultSubmissionStaleError("Match result submission preview is stale.")
        if target.status == MatchStatus.RESULT_CONFIRMED and command.reason is None:
            raise MatchResultSubmissionReasonRequiredError("Correcting a confirmed result requires a non-empty reason.")

        target_identity = tuple((entry.entry_id, entry.entry_number) for entry in target.entries)
        candidate_identity = tuple(
            sorted(
                ((entry.entry_id, entry.entry_number) for entry in command.candidate.entries),
                key=lambda value: value[1],
            )
        )
        if candidate_identity != target_identity:
            raise MatchResultSubmissionStaleError("Candidate Entries no longer match the current Match roster.")
        submitted_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        operation_type = (
            MatchResultSubmissionAuditType.SUBMITTED
            if target.next_revision_number == 1
            else MatchResultSubmissionAuditType.REVISED
        )
        try:
            saved = repository.persist_submission(
                command=command,
                target=target,
                operation_type=operation_type,
                submitted_at=submitted_at,
            )
        except MatchResultSubmissionError:
            raise
        except (TypeError, ValueError) as exc:
            raise MatchResultSubmissionInvalidSourceError(
                "Result submission did not produce complete canonical evidence."
            ) from exc
        if (
            saved.match_id != target.match_id
            or saved.revision_number != target.next_revision_number
            or saved.source_kind != command.source_kind
            or saved.candidate != command.candidate
            or saved.candidate_fingerprint != command.candidate.fingerprint
            or saved.superseded_submission_id != (target.pending.submission_id if target.pending is not None else None)
            or saved.corrects_confirmed != (target.confirmed is not None)
            or saved.submitted_at != submitted_at
        ):
            raise MatchResultSubmissionInvalidSourceError(
                "Persisted result submission receipt does not match the command."
            )
        return saved

    @staticmethod
    def _resolve_exact_retry(
        *,
        stored: StoredMatchResultSubmissionOperation,
        command: SaveMatchResultSubmission,
    ) -> SavedMatchResultSubmission:
        if (
            stored.type
            not in {
                MatchResultSubmissionAuditType.SUBMITTED.value,
                MatchResultSubmissionAuditType.REVISED.value,
            }
            or stored.request_fingerprint != command.request_fingerprint
            or stored.match_id != command.match_id
        ):
            raise MatchResultSubmissionIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise MatchResultSubmissionAuditError("Exact-retry operation has no stored after snapshot.")
        try:
            saved = SavedMatchResultSubmission.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise MatchResultSubmissionAuditError("Exact-retry operation has malformed stored evidence.") from exc
        expected_type = (
            MatchResultSubmissionAuditType.SUBMITTED.value
            if saved.revision_number == 1
            else MatchResultSubmissionAuditType.REVISED.value
        )
        if (
            saved.match_id != command.match_id
            or saved.source_kind != command.source_kind
            or saved.candidate != command.candidate
            or saved.candidate_fingerprint != command.candidate.fingerprint
            or stored.type != expected_type
        ):
            raise MatchResultSubmissionAuditError(
                "Exact-retry snapshot does not match its result submission operation."
            )
        return saved
