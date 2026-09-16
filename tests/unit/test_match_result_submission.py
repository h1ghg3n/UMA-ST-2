"""Native Match pending ResultSubmission Application tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner
from uma_st2.application.match import (
    MatchResultCandidate,
    MatchResultCandidateEntry,
    MatchResultRevisionReference,
    MatchResultSubmissionAuditType,
    MatchResultSubmissionCommands,
    MatchResultSubmissionEntry,
    MatchResultSubmissionIdempotencyConflictError,
    MatchResultSubmissionReasonRequiredError,
    MatchResultSubmissionStaleError,
    MatchResultSubmissionTarget,
    MatchResultSubmissionUnavailableError,
    SavedMatchResultSubmission,
    SaveMatchResultSubmission,
    StoredMatchResultSubmissionOperation,
)
from uma_st2.domain.match import (
    MatchResultSourceKind,
    MatchResultSubmissionStatus,
    MatchSourceKind,
    MatchStatus,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def _candidate(*, finish_time_ms: int | None = 119_300) -> MatchResultCandidate:
    return MatchResultCandidate(
        entries=(
            MatchResultCandidateEntry(
                entry_id=102,
                entry_number=2,
                rank=1,
                popularity_rank=1,
            ),
            MatchResultCandidateEntry(
                entry_id=101,
                entry_number=1,
                rank=2,
                popularity_rank=2,
                margin="목",
            ),
            MatchResultCandidateEntry(
                entry_id=103,
                entry_number=3,
                rank=3,
                popularity_rank=None,
                margin=None,
            ),
        ),
        finish_time_ms=finish_time_ms,
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
    next_revision_number: int = 1,
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
        next_revision_number=next_revision_number,
        pending=pending,
        confirmed=confirmed,
    )


def _command(
    target: MatchResultSubmissionTarget,
    *,
    key: str = "match-result-submit:555",
    candidate: MatchResultCandidate | None = None,
    reason: str | None = None,
) -> SaveMatchResultSubmission:
    return SaveMatchResultSubmission(
        match_id=target.match_id,
        expected_state_fingerprint=target.state_fingerprint,
        candidate=candidate or _candidate(),
        source_kind=MatchResultSourceKind.MANUAL,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        idempotency_key=key,
        correlation_id="555",
        reason=reason,
    )


class RecordingRepository:
    def __init__(
        self,
        target: MatchResultSubmissionTarget,
        *,
        stored: StoredMatchResultSubmissionOperation | None = None,
    ) -> None:
        self.target = target
        self.stored = stored
        self.calls: list[str] = []

    def lock_target(self, *, match_id: int) -> MatchResultSubmissionTarget | None:
        self.calls.append("lock_target")
        return self.target

    def find_operation(
        self,
        *,
        idempotency_key: str,
    ) -> StoredMatchResultSubmissionOperation | None:
        self.calls.append("find_operation")
        return self.stored

    def persist_submission(
        self,
        *,
        command: SaveMatchResultSubmission,
        target: MatchResultSubmissionTarget,
        operation_type: MatchResultSubmissionAuditType,
        submitted_at: datetime,
    ) -> SavedMatchResultSubmission:
        self.calls.append(f"persist:{operation_type.value}")
        return SavedMatchResultSubmission(
            submission_id=901,
            match_id=target.match_id,
            match_name=target.match_name,
            revision_number=target.next_revision_number,
            source_kind=command.source_kind,
            status=MatchResultSubmissionStatus.PENDING,
            candidate=command.candidate,
            candidate_fingerprint=command.candidate.fingerprint,
            superseded_submission_id=(target.pending.submission_id if target.pending is not None else None),
            corrects_confirmed=target.confirmed is not None,
            submitted_at=submitted_at,
        )


@dataclass
class RecordingUnitOfWork:
    match_result_submission: RecordingRepository
    commits: int = 0
    rollbacks: int = 0

    def __enter__(self) -> RecordingUnitOfWork:
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


class RecordingFactory:
    def __init__(self, repository: RecordingRepository) -> None:
        self.repository = repository
        self.created: list[RecordingUnitOfWork] = []

    def __call__(self) -> RecordingUnitOfWork:
        unit_of_work = RecordingUnitOfWork(self.repository)
        self.created.append(unit_of_work)
        return unit_of_work


def _commands(
    repository: RecordingRepository,
) -> tuple[MatchResultSubmissionCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return MatchResultSubmissionCommands(CommandRunner(factory), clock=lambda: NOW), factory


def test_initial_submit_persists_pending_without_materializing_result() -> None:
    target = _target()
    repository = RecordingRepository(target)
    commands, factory = _commands(repository)

    saved = commands.submit(_command(target))

    assert saved.revision_number == 1
    assert saved.status == MatchResultSubmissionStatus.PENDING
    assert saved.superseded_submission_id is None
    assert not saved.corrects_confirmed
    assert repository.calls == [
        "lock_target",
        "find_operation",
        "persist:match_result_submitted",
    ]
    assert factory.created[0].commits == 1


def test_pending_revision_is_superseded_by_revised_operation() -> None:
    pending = _reference(
        submission_id=801,
        revision_number=1,
        status=MatchResultSubmissionStatus.PENDING,
    )
    target = _target(next_revision_number=2, pending=pending)
    repository = RecordingRepository(target)

    saved = _commands(repository)[0].submit(_command(target, candidate=_candidate(finish_time_ms=None)))

    assert saved.revision_number == 2
    assert saved.superseded_submission_id == 801
    assert repository.calls[-1] == "persist:match_result_revised"


def test_confirmed_correction_requires_reason_and_preserves_authority() -> None:
    confirmed = _reference(
        submission_id=802,
        revision_number=1,
        status=MatchResultSubmissionStatus.CONFIRMED,
    )
    target = _target(
        status=MatchStatus.RESULT_CONFIRMED,
        next_revision_number=2,
        confirmed=confirmed,
    )
    repository = RecordingRepository(target)
    commands, factory = _commands(repository)

    with pytest.raises(MatchResultSubmissionReasonRequiredError):
        commands.submit(_command(target))
    assert repository.calls == ["lock_target", "find_operation"]
    assert factory.created[0].rollbacks == 1

    saved = commands.submit(_command(target, key="with-reason", reason="공식 정정"))
    assert saved.corrects_confirmed
    assert saved.superseded_submission_id is None


def test_stale_state_or_roster_mismatch_is_zero_write() -> None:
    target = _target()
    stale = SaveMatchResultSubmission(
        match_id=71,
        expected_state_fingerprint="a" * 64,
        candidate=_candidate(),
        source_kind=MatchResultSourceKind.MANUAL,
        actor_discord_user_id="1",
        guild_id="2",
        idempotency_key="stale",
    )
    repository = RecordingRepository(target)
    commands, _ = _commands(repository)
    with pytest.raises(MatchResultSubmissionStaleError):
        commands.submit(stale)

    mismatched = MatchResultCandidate(
        entries=(
            MatchResultCandidateEntry(101, 1, 1),
            MatchResultCandidateEntry(102, 2, 2),
            MatchResultCandidateEntry(999, 3, 3),
        )
    )
    with pytest.raises(MatchResultSubmissionStaleError):
        commands.submit(_command(target, key="roster", candidate=mismatched))
    assert all(not call.startswith("persist") for call in repository.calls)


@pytest.mark.parametrize(
    "target",
    (
        _target(status=MatchStatus.BETTING_OPEN),
        _target(source_kind=MatchSourceKind.IMPORTED_V1),
    ),
)
def test_ineligible_match_is_zero_write(target: MatchResultSubmissionTarget) -> None:
    repository = RecordingRepository(target)
    commands, factory = _commands(repository)

    with pytest.raises(MatchResultSubmissionUnavailableError):
        commands.submit(_command(target))

    assert repository.calls == ["lock_target", "find_operation"]
    assert factory.created[0].rollbacks == 1


def test_exact_retry_uses_stored_receipt_and_changed_payload_conflicts() -> None:
    target = _target()
    command = _command(target)
    committed = _commands(RecordingRepository(target))[0].submit(command)
    stored = StoredMatchResultSubmissionOperation(
        request_fingerprint=command.request_fingerprint,
        type=MatchResultSubmissionAuditType.SUBMITTED.value,
        match_id=target.match_id,
        after_data=committed.to_audit_payload(),
    )
    repository = RecordingRepository(target, stored=stored)
    commands, _ = _commands(repository)

    assert commands.submit(command) == committed
    assert repository.calls == ["lock_target", "find_operation"]

    with pytest.raises(MatchResultSubmissionIdempotencyConflictError):
        commands.submit(_command(target, key=command.idempotency_key, reason="다른 요청"))


def test_candidate_rejects_partial_duplicate_and_invalid_optional_fields() -> None:
    with pytest.raises(ValueError):
        MatchResultCandidate(
            entries=(
                MatchResultCandidateEntry(101, 1, 1),
                MatchResultCandidateEntry(102, 2, 3),
            )
        )
    with pytest.raises(ValueError):
        MatchResultCandidate(
            entries=(
                MatchResultCandidateEntry(101, 1, 1, popularity_rank=1),
                MatchResultCandidateEntry(102, 2, 2, popularity_rank=1),
            )
        )
    with pytest.raises(ValueError):
        MatchResultCandidate(
            entries=(MatchResultCandidateEntry(101, 1, 1, margin="목"),),
        )
    with pytest.raises(ValueError):
        MatchResultCandidate(
            entries=(MatchResultCandidateEntry(101, 1, 1),),
            finish_time_ms=119_350,
        )
