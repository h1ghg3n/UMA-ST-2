"""Native Match pending-result review and rejection Application tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.match import (
    MATCH_RESULT_REJECTED_AUDIT_TYPE,
    MatchResultCandidate,
    MatchResultCandidateEntry,
    MatchResultRejectionCommands,
    MatchResultReviewIdempotencyConflictError,
    MatchResultReviewQueries,
    MatchResultReviewStaleError,
    MatchResultReviewTargetChoice,
    MatchResultReviewUnavailableError,
    MatchResultRevisionReference,
    MatchResultSubmissionEntry,
    MatchResultSubmissionTarget,
    RejectedMatchResultSubmission,
    RejectMatchResultSubmission,
    StoredMatchResultRejectionOperation,
)
from uma_st2.domain.match import (
    MatchResultSourceKind,
    MatchResultSubmissionStatus,
    MatchSourceKind,
    MatchStatus,
)

NOW = datetime(2026, 8, 28, 13, 0, tzinfo=UTC)


def _candidate() -> MatchResultCandidate:
    return MatchResultCandidate(
        entries=(
            MatchResultCandidateEntry(102, 2, 1, popularity_rank=1),
            MatchResultCandidateEntry(101, 1, 2, popularity_rank=2, margin="목"),
            MatchResultCandidateEntry(103, 3, 3),
        ),
        finish_time_ms=92_300,
    )


def _reference(
    *,
    submission_id: int,
    revision_number: int,
    status: MatchResultSubmissionStatus,
) -> MatchResultRevisionReference:
    return MatchResultRevisionReference(
        submission_id=submission_id,
        revision_number=revision_number,
        status=status,
        source_kind=MatchResultSourceKind.MANUAL,
        candidate=_candidate(),
    )


def _target(
    *,
    status: MatchStatus = MatchStatus.BETTING_CLOSED,
    source_kind: MatchSourceKind = MatchSourceKind.NATIVE_V2,
    pending: MatchResultRevisionReference | None = None,
    confirmed: MatchResultRevisionReference | None = None,
) -> MatchResultSubmissionTarget:
    return MatchResultSubmissionTarget(
        match_id=71,
        match_name="제12회 @everyone 정기전",
        source_kind=source_kind,
        status=status,
        entries=(
            MatchResultSubmissionEntry(101, 1, "계정 1", "말 1"),
            MatchResultSubmissionEntry(102, 2, "계정 2", "말 2"),
            MatchResultSubmissionEntry(103, 3, "계정 3", "말 3"),
        ),
        next_revision_number=3,
        pending=pending,
        confirmed=confirmed,
    )


def _review_target(*, confirmed: bool = False) -> MatchResultSubmissionTarget:
    return _target(
        status=MatchStatus.RESULT_CONFIRMED if confirmed else MatchStatus.BETTING_CLOSED,
        pending=_reference(
            submission_id=902,
            revision_number=2,
            status=MatchResultSubmissionStatus.PENDING,
        ),
        confirmed=(
            _reference(
                submission_id=901,
                revision_number=1,
                status=MatchResultSubmissionStatus.CONFIRMED,
            )
            if confirmed
            else None
        ),
    )


def _command(
    target: MatchResultSubmissionTarget,
    *,
    key: str = "match-result-reject:555",
    reason: str = "공식 결과와 다름",
) -> RejectMatchResultSubmission:
    assert target.pending is not None
    return RejectMatchResultSubmission(
        match_id=target.match_id,
        submission_id=target.pending.submission_id,
        expected_state_fingerprint=target.state_fingerprint,
        expected_candidate_fingerprint=target.pending.candidate.fingerprint,
        reason=reason,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        idempotency_key=key,
        correlation_id="555",
    )


class RecordingRepository:
    def __init__(
        self,
        target: MatchResultSubmissionTarget,
        *,
        stored: StoredMatchResultRejectionOperation | None = None,
    ) -> None:
        self.target = target
        self.stored = stored
        self.calls: list[str] = []

    def lock_target(self, *, match_id: int) -> MatchResultSubmissionTarget | None:
        self.calls.append("lock_target")
        return self.target

    def find_operation(self, *, idempotency_key: str) -> StoredMatchResultRejectionOperation | None:
        self.calls.append("find_operation")
        return self.stored

    def persist_rejection(
        self,
        *,
        command: RejectMatchResultSubmission,
        target: MatchResultSubmissionTarget,
        rejected_at: datetime,
    ) -> RejectedMatchResultSubmission:
        self.calls.append("persist_rejection")
        assert target.pending is not None
        return RejectedMatchResultSubmission(
            submission_id=target.pending.submission_id,
            match_id=target.match_id,
            match_name=target.match_name,
            revision_number=target.pending.revision_number,
            source_kind=target.pending.source_kind,
            status=MatchResultSubmissionStatus.REJECTED,
            candidate_fingerprint=target.pending.candidate.fingerprint,
            reason=command.reason,
            rejected_at=rejected_at,
            preserved_confirmed_submission_id=(
                target.confirmed.submission_id if target.confirmed is not None else None
            ),
        )


class RecordingQueryRepository:
    def __init__(self, target: MatchResultSubmissionTarget | None) -> None:
        self.target = target
        self.calls: list[tuple[object, ...]] = []

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchResultReviewTargetChoice, ...]:
        self.calls.append(("search", search, limit))
        return ()

    def get_target(self, *, match_id: int) -> MatchResultSubmissionTarget | None:
        self.calls.append(("get", match_id))
        return self.target


@dataclass
class RecordingCommandUnitOfWork:
    match_result_rejection: RecordingRepository
    commits: int = 0
    rollbacks: int = 0

    def __enter__(self) -> RecordingCommandUnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if self.commits == 0 and self.rollbacks == 0:
            self.rollbacks += 1
        return False

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


@dataclass
class RecordingQueryUnitOfWork:
    match_result_review_queries: RecordingQueryRepository
    commits: int = 0
    rollbacks: int = 0

    def __enter__(self) -> RecordingQueryUnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if self.commits == 0 and self.rollbacks == 0:
            self.rollbacks += 1
        return False

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _commands(
    repository: RecordingRepository,
) -> tuple[MatchResultRejectionCommands, list[RecordingCommandUnitOfWork]]:
    created: list[RecordingCommandUnitOfWork] = []

    def factory() -> RecordingCommandUnitOfWork:
        unit_of_work = RecordingCommandUnitOfWork(repository)
        created.append(unit_of_work)
        return unit_of_work

    return MatchResultRejectionCommands(CommandRunner(factory), clock=lambda: NOW), created


def _queries(repository: RecordingQueryRepository) -> MatchResultReviewQueries:
    return MatchResultReviewQueries(QueryRunner(lambda: RecordingQueryUnitOfWork(repository)))


def test_review_query_returns_closed_current_pending_without_write() -> None:
    target = _review_target()
    repository = RecordingQueryRepository(target)

    assert _queries(repository).get_target(match_id=71) == target
    assert repository.calls == [("get", 71)]

    unavailable = RecordingQueryRepository(None)
    with pytest.raises(MatchResultReviewUnavailableError):
        _queries(unavailable).get_target(match_id=71)


def test_rejection_commits_current_pending_and_preserves_confirmed_authority() -> None:
    target = _review_target(confirmed=True)
    repository = RecordingRepository(target)
    commands, created = _commands(repository)

    rejected = commands.reject(_command(target))

    assert rejected.submission_id == 902
    assert rejected.status == MatchResultSubmissionStatus.REJECTED
    assert rejected.preserved_confirmed_submission_id == 901
    assert repository.calls == ["lock_target", "find_operation", "persist_rejection"]
    assert created[0].commits == 1


def test_stale_or_ineligible_rejection_is_zero_write() -> None:
    target = _review_target()
    repository = RecordingRepository(target)
    commands, created = _commands(repository)
    stale = RejectMatchResultSubmission(
        match_id=target.match_id,
        submission_id=902,
        expected_state_fingerprint="a" * 64,
        expected_candidate_fingerprint=target.pending.candidate.fingerprint,  # type: ignore[union-attr]
        reason="오류",
        actor_discord_user_id="1",
        guild_id="2",
        idempotency_key="stale",
    )
    with pytest.raises(MatchResultReviewStaleError):
        commands.reject(stale)
    assert repository.calls == ["lock_target", "find_operation"]
    assert created[0].rollbacks == 1

    no_pending = _target()
    no_pending_repository = RecordingRepository(no_pending)
    no_pending_commands, _ = _commands(no_pending_repository)
    with pytest.raises(MatchResultReviewUnavailableError):
        no_pending_commands.reject(
            RejectMatchResultSubmission(
                match_id=71,
                submission_id=902,
                expected_state_fingerprint=no_pending.state_fingerprint,
                expected_candidate_fingerprint=_candidate().fingerprint,
                reason="오류",
                actor_discord_user_id="1",
                guild_id="2",
                idempotency_key="no-pending",
            )
        )

    imported = _target(
        source_kind=MatchSourceKind.IMPORTED_V1,
        pending=target.pending,
    )
    imported_commands, _ = _commands(RecordingRepository(imported))
    with pytest.raises(MatchResultReviewUnavailableError):
        imported_commands.reject(_command(imported, key="imported"))


def test_exact_retry_uses_rejection_receipt_and_changed_reason_conflicts() -> None:
    target = _review_target()
    command = _command(target)
    committed = _commands(RecordingRepository(target))[0].reject(command)
    stored = StoredMatchResultRejectionOperation(
        request_fingerprint=command.request_fingerprint,
        type=MATCH_RESULT_REJECTED_AUDIT_TYPE,
        match_id=target.match_id,
        after_data=committed.to_audit_payload(),
    )
    post_rejection_target = _target()
    repository = RecordingRepository(post_rejection_target, stored=stored)
    commands, _ = _commands(repository)

    assert commands.reject(command) == committed
    assert repository.calls == ["lock_target", "find_operation"]

    with pytest.raises(MatchResultReviewIdempotencyConflictError):
        commands.reject(_command(target, key=command.idempotency_key, reason="다른 사유"))


def test_rejection_reason_is_required_and_receipt_audit_round_trips() -> None:
    target = _review_target()
    with pytest.raises(ValueError, match="reason"):
        _command(target, reason="   ")

    receipt = _commands(RecordingRepository(target))[0].reject(_command(target))
    assert RejectedMatchResultSubmission.from_audit_payload(receipt.to_audit_payload()) == receipt
