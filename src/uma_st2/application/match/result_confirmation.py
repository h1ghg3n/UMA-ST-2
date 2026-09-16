"""Native V2 Match pending-result authoritative confirmation boundary."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
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

from .result_submission import MatchResultCandidate, MatchResultSubmissionTarget

MATCH_RESULT_CONFIRMATION_AUDIT_SCHEMA_VERSION: Final = 1
MATCH_RESULT_CONFIRMED_AUDIT_TYPE: Final = "match_result_confirmed"


class MatchResultConfirmationError(ValueError):
    """Base error for authoritative pending-result confirmation."""


class MatchResultConfirmationUnavailableError(MatchResultConfirmationError):
    """The Match has no confirmable current pending result."""


class MatchResultConfirmationStaleError(MatchResultConfirmationError):
    """The previewed pending result is no longer current."""


class MatchResultConfirmationIdempotencyConflictError(MatchResultConfirmationError):
    """An idempotency key belongs to another logical confirmation."""


class MatchResultConfirmationInvalidSourceError(MatchResultConfirmationError):
    """Stored Match, Entry, revision, or marker facts are malformed."""


class MatchResultConfirmationAuditError(MatchResultConfirmationError):
    """Stored confirmation exact-retry evidence is absent or malformed."""


def _positive_int(value: int, *, field_name: str) -> int:
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
    return _positive_int(payload[key], field_name=key)  # type: ignore[arg-type]


def _payload_optional_positive_int(payload: Mapping[str, object], key: str) -> int | None:
    value = payload.get(key)
    return None if value is None else _positive_int(value, field_name=key)  # type: ignore[arg-type]


def _payload_string(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string.")
    return value


@dataclass(frozen=True, slots=True)
class ConfirmMatchResultSubmission:
    """Final request to promote one current pending revision to authority."""

    match_id: int
    submission_id: int
    expected_state_fingerprint: str
    expected_candidate_fingerprint: str
    actor_discord_user_id: str
    guild_id: str
    idempotency_key: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        _positive_int(self.match_id, field_name="match_id")
        _positive_int(self.submission_id, field_name="submission_id")
        for field_name, max_length, optional in (
            ("expected_state_fingerprint", 64, False),
            ("expected_candidate_fingerprint", 64, False),
            ("actor_discord_user_id", 32, False),
            ("guild_id", 32, False),
            ("idempotency_key", 128, False),
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
                "schema": "match-result-confirmation-command-v1",
                "match_id": self.match_id,
                "submission_id": self.submission_id,
                "expected_state_fingerprint": self.expected_state_fingerprint,
                "expected_candidate_fingerprint": self.expected_candidate_fingerprint,
                "actor_discord_user_id": self.actor_discord_user_id,
                "guild_id": self.guild_id,
            }
        )


@dataclass(frozen=True, slots=True)
class ConfirmedMatchResultSubmission:
    """Committed authority and materialized-board receipt."""

    submission_id: int
    match_id: int
    match_name: str
    revision_number: int
    source_kind: MatchResultSourceKind
    status: MatchResultSubmissionStatus
    match_status: MatchStatus
    candidate: MatchResultCandidate
    candidate_fingerprint: str
    previous_confirmed_submission_id: int | None
    confirmed_at: datetime

    def __post_init__(self) -> None:
        _positive_int(self.submission_id, field_name="submission_id")
        _positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        _positive_int(self.revision_number, field_name="revision_number")
        object.__setattr__(self, "source_kind", MatchResultSourceKind(self.source_kind))
        object.__setattr__(self, "status", MatchResultSubmissionStatus(self.status))
        if self.status != MatchResultSubmissionStatus.CONFIRMED:
            raise ValueError("A confirmation receipt must have confirmed status.")
        object.__setattr__(self, "match_status", MatchStatus(self.match_status))
        if self.match_status != MatchStatus.RESULT_CONFIRMED:
            raise ValueError("A confirmation receipt must have result-confirmed Match status.")
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
        if self.previous_confirmed_submission_id is not None:
            _positive_int(
                self.previous_confirmed_submission_id,
                field_name="previous_confirmed_submission_id",
            )
            if self.previous_confirmed_submission_id == self.submission_id:
                raise ValueError("Previous and current confirmed submissions must differ.")
        object.__setattr__(
            self,
            "confirmed_at",
            normalize_utc_datetime(self.confirmed_at, field_name="confirmed_at"),
        )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": MATCH_RESULT_CONFIRMATION_AUDIT_SCHEMA_VERSION,
            "submission_id": self.submission_id,
            "match_id": self.match_id,
            "match_name": self.match_name,
            "revision_number": self.revision_number,
            "source_kind": self.source_kind.value,
            "status": self.status.value,
            "match_status": self.match_status.value,
            "candidate": self.candidate.to_payload(),
            "candidate_fingerprint": self.candidate_fingerprint,
            "previous_confirmed_submission_id": self.previous_confirmed_submission_id,
            "confirmed_at": self.confirmed_at.isoformat(),
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> ConfirmedMatchResultSubmission:
        if payload.get("schema_version") != MATCH_RESULT_CONFIRMATION_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported Match result confirmation audit schema version.")
        candidate_payload = payload.get("candidate")
        if not isinstance(candidate_payload, Mapping):
            raise ValueError("candidate must be an object.")
        return cls(
            submission_id=_payload_positive_int(payload, "submission_id"),
            match_id=_payload_positive_int(payload, "match_id"),
            match_name=_payload_string(payload, "match_name"),
            revision_number=_payload_positive_int(payload, "revision_number"),
            source_kind=MatchResultSourceKind(_payload_string(payload, "source_kind")),
            status=MatchResultSubmissionStatus(_payload_string(payload, "status")),
            match_status=MatchStatus(_payload_string(payload, "match_status")),
            candidate=MatchResultCandidate.from_payload(candidate_payload),
            candidate_fingerprint=_payload_string(payload, "candidate_fingerprint"),
            previous_confirmed_submission_id=_payload_optional_positive_int(
                payload,
                "previous_confirmed_submission_id",
            ),
            confirmed_at=datetime.fromisoformat(_payload_string(payload, "confirmed_at")),
        )


@dataclass(frozen=True, slots=True)
class StoredMatchResultConfirmationOperation:
    """Minimal exact-retry operation evidence."""

    request_fingerprint: str | None
    type: str | None
    match_id: int | None
    after_data: Mapping[str, object] | None


class MatchResultConfirmationRepository(Protocol):
    """Persistence operations for confirming one current pending revision."""

    def lock_target(self, *, match_id: int) -> MatchResultSubmissionTarget | None: ...

    def find_operation(
        self,
        *,
        idempotency_key: str,
    ) -> StoredMatchResultConfirmationOperation | None: ...

    def persist_confirmation(
        self,
        *,
        command: ConfirmMatchResultSubmission,
        target: MatchResultSubmissionTarget,
        confirmed_at: datetime,
    ) -> ConfirmedMatchResultSubmission: ...


class MatchResultConfirmationUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing authoritative result confirmation persistence."""

    @property
    def match_result_confirmation(self) -> MatchResultConfirmationRepository: ...


@dataclass(frozen=True, slots=True)
class MatchResultConfirmationCommands:
    """Application entry point for explicit pending-result confirmation."""

    command_runner: CommandRunner[MatchResultConfirmationUnitOfWork]
    clock: Callable[[], datetime]

    def confirm(self, command: ConfirmMatchResultSubmission) -> ConfirmedMatchResultSubmission:
        return self.command_runner.run(
            lambda unit_of_work: self._confirm(unit_of_work.match_result_confirmation, command)
        )

    def _confirm(
        self,
        repository: MatchResultConfirmationRepository,
        command: ConfirmMatchResultSubmission,
    ) -> ConfirmedMatchResultSubmission:
        try:
            target = repository.lock_target(match_id=command.match_id)
        except (TypeError, ValueError) as exc:
            raise MatchResultConfirmationInvalidSourceError(
                "Stored Match result confirmation facts are malformed."
            ) from exc
        if target is None:
            raise MatchResultConfirmationUnavailableError("Match does not exist.")

        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)

        if target.source_kind != MatchSourceKind.NATIVE_V2 or target.status not in {
            MatchStatus.BETTING_CLOSED,
            MatchStatus.RESULT_CONFIRMED,
        }:
            raise MatchResultConfirmationUnavailableError(
                "Result confirmation requires a native betting-closed or result-confirmed Match."
            )
        pending = target.pending
        if pending is None:
            raise MatchResultConfirmationUnavailableError("Match has no current pending result to confirm.")
        if (
            target.state_fingerprint != command.expected_state_fingerprint
            or pending.submission_id != command.submission_id
            or pending.candidate.fingerprint != command.expected_candidate_fingerprint
        ):
            raise MatchResultConfirmationStaleError("Pending result confirmation is stale.")

        confirmed_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        try:
            confirmed = repository.persist_confirmation(
                command=command,
                target=target,
                confirmed_at=confirmed_at,
            )
        except MatchResultConfirmationError:
            raise
        except (TypeError, ValueError) as exc:
            raise MatchResultConfirmationInvalidSourceError(
                "Result confirmation did not produce complete canonical evidence."
            ) from exc
        if (
            confirmed.submission_id != pending.submission_id
            or confirmed.match_id != target.match_id
            or confirmed.match_name != target.match_name
            or confirmed.revision_number != pending.revision_number
            or confirmed.source_kind != pending.source_kind
            or confirmed.candidate != pending.candidate
            or confirmed.candidate_fingerprint != pending.candidate.fingerprint
            or confirmed.previous_confirmed_submission_id
            != (target.confirmed.submission_id if target.confirmed is not None else None)
            or confirmed.confirmed_at != confirmed_at
        ):
            raise MatchResultConfirmationInvalidSourceError(
                "Persisted result confirmation receipt does not match the command."
            )
        return confirmed

    @staticmethod
    def _resolve_exact_retry(
        *,
        stored: StoredMatchResultConfirmationOperation,
        command: ConfirmMatchResultSubmission,
    ) -> ConfirmedMatchResultSubmission:
        if (
            stored.type != MATCH_RESULT_CONFIRMED_AUDIT_TYPE
            or stored.request_fingerprint != command.request_fingerprint
            or stored.match_id != command.match_id
        ):
            raise MatchResultConfirmationIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise MatchResultConfirmationAuditError("Exact-retry confirmation has no stored after snapshot.")
        try:
            confirmed = ConfirmedMatchResultSubmission.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise MatchResultConfirmationAuditError("Exact-retry confirmation has malformed stored evidence.") from exc
        if (
            confirmed.match_id != command.match_id
            or confirmed.submission_id != command.submission_id
            or confirmed.candidate_fingerprint != command.expected_candidate_fingerprint
        ):
            raise MatchResultConfirmationAuditError("Exact-retry confirmation snapshot does not match its command.")
        return confirmed
