"""Native V2 Match pending-result review and rejection boundaries."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, QueryRunner, UnitOfWork
from uma_st2.domain.match import (
    MatchResultSourceKind,
    MatchResultSubmissionStatus,
    MatchSourceKind,
    MatchStatus,
)
from uma_st2.shared import normalize_utc_datetime

from .result_submission import MatchResultSubmissionTarget

MATCH_RESULT_REJECTION_AUDIT_SCHEMA_VERSION: Final = 1
MATCH_RESULT_REJECTED_AUDIT_TYPE: Final = "match_result_rejected"


class MatchResultReviewError(ValueError):
    """Base error for pending result review and rejection."""


class MatchResultReviewUnavailableError(MatchResultReviewError):
    """The Match has no reviewable current pending result."""


class MatchResultReviewStaleError(MatchResultReviewError):
    """The previewed pending result is no longer current."""


class MatchResultReviewIdempotencyConflictError(MatchResultReviewError):
    """An idempotency key belongs to another logical rejection."""


class MatchResultReviewInvalidSourceError(MatchResultReviewError):
    """Stored Match, Entry, revision, or marker facts are malformed."""


class MatchResultReviewAuditError(MatchResultReviewError):
    """Stored rejection exact-retry evidence is absent or malformed."""


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


def _payload_string(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string.")
    return value


@dataclass(frozen=True, slots=True)
class MatchResultReviewTargetChoice:
    """One bounded current-pending Match autocomplete row."""

    match_id: int
    match_name: str
    match_status: MatchStatus
    submission_id: int
    revision_number: int
    source_kind: MatchResultSourceKind
    entry_count: int

    def __post_init__(self) -> None:
        _positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self,
            "match_name",
            _normalized_string(self.match_name, field_name="match_name", max_length=200),
        )
        object.__setattr__(self, "match_status", MatchStatus(self.match_status))
        if self.match_status not in {MatchStatus.BETTING_CLOSED, MatchStatus.RESULT_CONFIRMED}:
            raise ValueError("match_status must be review eligible.")
        _positive_int(self.submission_id, field_name="submission_id")
        _positive_int(self.revision_number, field_name="revision_number")
        object.__setattr__(self, "source_kind", MatchResultSourceKind(self.source_kind))
        _positive_int(self.entry_count, field_name="entry_count")


@dataclass(frozen=True, slots=True)
class RejectMatchResultSubmission:
    """Final current-pending rejection request."""

    match_id: int
    submission_id: int
    expected_state_fingerprint: str
    expected_candidate_fingerprint: str
    reason: str
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
            ("reason", 255, False),
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
                "schema": "match-result-rejection-command-v1",
                "match_id": self.match_id,
                "submission_id": self.submission_id,
                "expected_state_fingerprint": self.expected_state_fingerprint,
                "expected_candidate_fingerprint": self.expected_candidate_fingerprint,
                "reason": self.reason,
                "actor_discord_user_id": self.actor_discord_user_id,
                "guild_id": self.guild_id,
            }
        )


@dataclass(frozen=True, slots=True)
class RejectedMatchResultSubmission:
    """Committed current-pending rejection receipt."""

    submission_id: int
    match_id: int
    match_name: str
    revision_number: int
    source_kind: MatchResultSourceKind
    status: MatchResultSubmissionStatus
    candidate_fingerprint: str
    reason: str
    rejected_at: datetime
    preserved_confirmed_submission_id: int | None

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
        if self.status != MatchResultSubmissionStatus.REJECTED:
            raise ValueError("A rejection receipt must have rejected status.")
        for field_name, max_length in (("candidate_fingerprint", 64), ("reason", 255)):
            object.__setattr__(
                self,
                field_name,
                _normalized_string(getattr(self, field_name), field_name=field_name, max_length=max_length),
            )
        object.__setattr__(
            self,
            "rejected_at",
            normalize_utc_datetime(self.rejected_at, field_name="rejected_at"),
        )
        if self.preserved_confirmed_submission_id is not None:
            _positive_int(
                self.preserved_confirmed_submission_id,
                field_name="preserved_confirmed_submission_id",
            )

    def to_audit_payload(self) -> dict[str, object]:
        return {
            "schema_version": MATCH_RESULT_REJECTION_AUDIT_SCHEMA_VERSION,
            "submission_id": self.submission_id,
            "match_id": self.match_id,
            "match_name": self.match_name,
            "revision_number": self.revision_number,
            "source_kind": self.source_kind.value,
            "status": self.status.value,
            "candidate_fingerprint": self.candidate_fingerprint,
            "reason": self.reason,
            "rejected_at": self.rejected_at.isoformat(),
            "preserved_confirmed_submission_id": self.preserved_confirmed_submission_id,
        }

    @classmethod
    def from_audit_payload(cls, payload: Mapping[str, object]) -> RejectedMatchResultSubmission:
        if payload.get("schema_version") != MATCH_RESULT_REJECTION_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported Match result rejection audit schema version.")
        preserved = payload.get("preserved_confirmed_submission_id")
        return cls(
            submission_id=_payload_positive_int(payload, "submission_id"),
            match_id=_payload_positive_int(payload, "match_id"),
            match_name=_payload_string(payload, "match_name"),
            revision_number=_payload_positive_int(payload, "revision_number"),
            source_kind=MatchResultSourceKind(_payload_string(payload, "source_kind")),
            status=MatchResultSubmissionStatus(_payload_string(payload, "status")),
            candidate_fingerprint=_payload_string(payload, "candidate_fingerprint"),
            reason=_payload_string(payload, "reason"),
            rejected_at=datetime.fromisoformat(_payload_string(payload, "rejected_at")),
            preserved_confirmed_submission_id=(
                None if preserved is None else _positive_int(preserved, field_name="preserved_confirmed_submission_id")  # type: ignore[arg-type]
            ),
        )


@dataclass(frozen=True, slots=True)
class StoredMatchResultRejectionOperation:
    """Minimal exact-retry operation evidence."""

    request_fingerprint: str | None
    type: str | None
    match_id: int | None
    after_data: Mapping[str, object] | None


class MatchResultReviewQueryRepository(Protocol):
    """Read-only persistence operations for current pending review."""

    def search_targets(
        self,
        *,
        search: str,
        limit: int,
    ) -> tuple[MatchResultReviewTargetChoice, ...]: ...

    def get_target(self, *, match_id: int) -> MatchResultSubmissionTarget | None: ...


class MatchResultReviewQueryUnitOfWork(UnitOfWork, Protocol):
    """Read-only UoW exposing pending-result review projections."""

    @property
    def match_result_review_queries(self) -> MatchResultReviewQueryRepository: ...


@dataclass(frozen=True, slots=True)
class MatchResultReviewQueries:
    """Application entry point for private current-pending review."""

    query_runner: QueryRunner[MatchResultReviewQueryUnitOfWork]

    def search_targets(
        self,
        *,
        search: str,
        limit: int = 25,
    ) -> tuple[MatchResultReviewTargetChoice, ...]:
        if not isinstance(search, str):
            raise ValueError("search must be a string.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be between 1 and 25.")
        normalized = search.strip()

        def query(
            unit_of_work: MatchResultReviewQueryUnitOfWork,
        ) -> tuple[MatchResultReviewTargetChoice, ...]:
            try:
                return unit_of_work.match_result_review_queries.search_targets(
                    search=normalized,
                    limit=limit,
                )
            except (TypeError, ValueError) as exc:
                raise MatchResultReviewInvalidSourceError(
                    "Stored Match result review target list is malformed."
                ) from exc

        return self.query_runner.run(query)

    def get_target(self, *, match_id: int) -> MatchResultSubmissionTarget:
        _positive_int(match_id, field_name="match_id")

        def query(unit_of_work: MatchResultReviewQueryUnitOfWork) -> MatchResultSubmissionTarget:
            try:
                target = unit_of_work.match_result_review_queries.get_target(match_id=match_id)
            except (TypeError, ValueError) as exc:
                raise MatchResultReviewInvalidSourceError("Stored Match result review target is malformed.") from exc
            if target is None:
                raise MatchResultReviewUnavailableError("Match has no current pending result available for review.")
            return target

        return self.query_runner.run(query)


class MatchResultRejectionRepository(Protocol):
    """Persistence operations for rejecting one current pending revision."""

    def lock_target(self, *, match_id: int) -> MatchResultSubmissionTarget | None: ...

    def find_operation(
        self,
        *,
        idempotency_key: str,
    ) -> StoredMatchResultRejectionOperation | None: ...

    def persist_rejection(
        self,
        *,
        command: RejectMatchResultSubmission,
        target: MatchResultSubmissionTarget,
        rejected_at: datetime,
    ) -> RejectedMatchResultSubmission: ...


class MatchResultRejectionUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing current-pending rejection persistence."""

    @property
    def match_result_rejection(self) -> MatchResultRejectionRepository: ...


@dataclass(frozen=True, slots=True)
class MatchResultRejectionCommands:
    """Application entry point for reason-required pending rejection."""

    command_runner: CommandRunner[MatchResultRejectionUnitOfWork]
    clock: Callable[[], datetime]

    def reject(self, command: RejectMatchResultSubmission) -> RejectedMatchResultSubmission:
        return self.command_runner.run(lambda unit_of_work: self._reject(unit_of_work.match_result_rejection, command))

    def _reject(
        self,
        repository: MatchResultRejectionRepository,
        command: RejectMatchResultSubmission,
    ) -> RejectedMatchResultSubmission:
        try:
            target = repository.lock_target(match_id=command.match_id)
        except (TypeError, ValueError) as exc:
            raise MatchResultReviewInvalidSourceError("Stored Match result review facts are malformed.") from exc
        if target is None:
            raise MatchResultReviewUnavailableError("Match does not exist.")

        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)

        if target.source_kind != MatchSourceKind.NATIVE_V2 or target.status not in {
            MatchStatus.BETTING_CLOSED,
            MatchStatus.RESULT_CONFIRMED,
        }:
            raise MatchResultReviewUnavailableError(
                "Result rejection requires a native betting-closed or result-confirmed Match."
            )
        pending = target.pending
        if pending is None:
            raise MatchResultReviewUnavailableError("Match has no current pending result to reject.")
        if (
            target.state_fingerprint != command.expected_state_fingerprint
            or pending.submission_id != command.submission_id
            or pending.candidate.fingerprint != command.expected_candidate_fingerprint
        ):
            raise MatchResultReviewStaleError("Pending result review is stale.")

        rejected_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        try:
            rejected = repository.persist_rejection(
                command=command,
                target=target,
                rejected_at=rejected_at,
            )
        except MatchResultReviewError:
            raise
        except (TypeError, ValueError) as exc:
            raise MatchResultReviewInvalidSourceError(
                "Result rejection did not produce complete canonical evidence."
            ) from exc
        if (
            rejected.submission_id != pending.submission_id
            or rejected.match_id != target.match_id
            or rejected.match_name != target.match_name
            or rejected.revision_number != pending.revision_number
            or rejected.source_kind != pending.source_kind
            or rejected.candidate_fingerprint != pending.candidate.fingerprint
            or rejected.reason != command.reason
            or rejected.rejected_at != rejected_at
            or rejected.preserved_confirmed_submission_id
            != (target.confirmed.submission_id if target.confirmed is not None else None)
        ):
            raise MatchResultReviewInvalidSourceError("Persisted result rejection receipt does not match the command.")
        return rejected

    @staticmethod
    def _resolve_exact_retry(
        *,
        stored: StoredMatchResultRejectionOperation,
        command: RejectMatchResultSubmission,
    ) -> RejectedMatchResultSubmission:
        if (
            stored.type != MATCH_RESULT_REJECTED_AUDIT_TYPE
            or stored.request_fingerprint != command.request_fingerprint
            or stored.match_id != command.match_id
        ):
            raise MatchResultReviewIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            )
        if stored.after_data is None:
            raise MatchResultReviewAuditError("Exact-retry rejection has no stored after snapshot.")
        try:
            rejected = RejectedMatchResultSubmission.from_audit_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise MatchResultReviewAuditError("Exact-retry rejection has malformed stored evidence.") from exc
        if (
            rejected.match_id != command.match_id
            or rejected.submission_id != command.submission_id
            or rejected.candidate_fingerprint != command.expected_candidate_fingerprint
            or rejected.reason != command.reason
        ):
            raise MatchResultReviewAuditError("Exact-retry rejection snapshot does not match its command.")
        return rejected
