"""Native Match pending-result confirmation Application tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner
from uma_st2.application.match import (
    MATCH_RESULT_CONFIRMED_AUDIT_TYPE,
    ConfirmedMatchResultSubmission,
    ConfirmMatchResultSubmission,
    MatchResultCandidate,
    MatchResultCandidateEntry,
    MatchResultConfirmationCommands,
    MatchResultConfirmationIdempotencyConflictError,
    MatchResultConfirmationStaleError,
    MatchResultConfirmationUnavailableError,
    MatchResultRevisionReference,
    MatchResultSubmissionEntry,
    MatchResultSubmissionTarget,
    StoredMatchResultConfirmationOperation,
)
from uma_st2.domain.match import (
    MatchResultSourceKind,
    MatchResultSubmissionStatus,
    MatchSourceKind,
    MatchStatus,
)

NOW = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)


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
    pending: bool = True,
    confirmed: bool = False,
) -> MatchResultSubmissionTarget:
    return MatchResultSubmissionTarget(
        match_id=71,
        match_name="제12회 정기전",
        source_kind=source_kind,
        status=status,
        entries=(
            MatchResultSubmissionEntry(101, 1, "계정 1", "말 1"),
            MatchResultSubmissionEntry(102, 2, "계정 2", "말 2"),
            MatchResultSubmissionEntry(103, 3, "계정 3", "말 3"),
        ),
        next_revision_number=3,
        pending=(
            _reference(
                submission_id=902,
                revision_number=2,
                status=MatchResultSubmissionStatus.PENDING,
            )
            if pending
            else None
        ),
        confirmed=(
            _reference(
                submission_id=901 if pending else 902,
                revision_number=1 if pending else 2,
                status=MatchResultSubmissionStatus.CONFIRMED,
            )
            if confirmed
            else None
        ),
    )


def _command(
    target: MatchResultSubmissionTarget,
    *,
    key: str = "match-result-confirm:555",
) -> ConfirmMatchResultSubmission:
    assert target.pending is not None
    return ConfirmMatchResultSubmission(
        match_id=target.match_id,
        submission_id=target.pending.submission_id,
        expected_state_fingerprint=target.state_fingerprint,
        expected_candidate_fingerprint=target.pending.candidate.fingerprint,
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
        stored: StoredMatchResultConfirmationOperation | None = None,
    ) -> None:
        self.target = target
        self.stored = stored
        self.calls: list[str] = []

    def lock_target(self, *, match_id: int) -> MatchResultSubmissionTarget | None:
        self.calls.append("lock_target")
        return self.target

    def find_operation(self, *, idempotency_key: str) -> StoredMatchResultConfirmationOperation | None:
        self.calls.append("find_operation")
        return self.stored

    def persist_confirmation(
        self,
        *,
        command: ConfirmMatchResultSubmission,
        target: MatchResultSubmissionTarget,
        confirmed_at: datetime,
    ) -> ConfirmedMatchResultSubmission:
        self.calls.append("persist_confirmation")
        assert target.pending is not None
        return ConfirmedMatchResultSubmission(
            submission_id=target.pending.submission_id,
            match_id=target.match_id,
            match_name=target.match_name,
            revision_number=target.pending.revision_number,
            source_kind=target.pending.source_kind,
            status=MatchResultSubmissionStatus.CONFIRMED,
            match_status=MatchStatus.RESULT_CONFIRMED,
            candidate=target.pending.candidate,
            candidate_fingerprint=target.pending.candidate.fingerprint,
            previous_confirmed_submission_id=(target.confirmed.submission_id if target.confirmed is not None else None),
            confirmed_at=confirmed_at,
        )


@dataclass
class RecordingUnitOfWork:
    match_result_confirmation: RecordingRepository
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


def _commands(repository: RecordingRepository) -> tuple[MatchResultConfirmationCommands, list[RecordingUnitOfWork]]:
    created: list[RecordingUnitOfWork] = []

    def factory() -> RecordingUnitOfWork:
        unit_of_work = RecordingUnitOfWork(repository)
        created.append(unit_of_work)
        return unit_of_work

    return MatchResultConfirmationCommands(CommandRunner(factory), clock=lambda: NOW), created


def test_initial_confirmation_materializes_current_pending_authority() -> None:
    target = _target()
    repository = RecordingRepository(target)
    commands, units = _commands(repository)

    confirmed = commands.confirm(_command(target))

    assert confirmed.submission_id == 902
    assert confirmed.status == MatchResultSubmissionStatus.CONFIRMED
    assert confirmed.match_status == MatchStatus.RESULT_CONFIRMED
    assert confirmed.previous_confirmed_submission_id is None
    assert confirmed.candidate == _candidate()
    assert repository.calls == ["lock_target", "find_operation", "persist_confirmation"]
    assert units[0].commits == 1


def test_confirmed_correction_supersedes_previous_authority() -> None:
    target = _target(status=MatchStatus.RESULT_CONFIRMED, confirmed=True)
    confirmed = _commands(RecordingRepository(target))[0].confirm(_command(target))

    assert confirmed.previous_confirmed_submission_id == 901
    assert ConfirmedMatchResultSubmission.from_audit_payload(confirmed.to_audit_payload()) == confirmed


def test_stale_imported_and_missing_pending_confirmation_are_zero_write() -> None:
    target = _target()
    stale = ConfirmMatchResultSubmission(
        match_id=target.match_id,
        submission_id=999,
        expected_state_fingerprint=target.state_fingerprint,
        expected_candidate_fingerprint=target.pending.candidate.fingerprint,  # type: ignore[union-attr]
        actor_discord_user_id="1",
        guild_id="2",
        idempotency_key="stale",
    )
    repository = RecordingRepository(target)
    commands, units = _commands(repository)
    with pytest.raises(MatchResultConfirmationStaleError):
        commands.confirm(stale)
    assert repository.calls == ["lock_target", "find_operation"]
    assert units[0].rollbacks == 1

    imported = _target(source_kind=MatchSourceKind.IMPORTED_V1)
    with pytest.raises(MatchResultConfirmationUnavailableError):
        _commands(RecordingRepository(imported))[0].confirm(_command(imported, key="imported"))

    no_pending = _target(status=MatchStatus.RESULT_CONFIRMED, pending=False, confirmed=True)
    no_pending_command = ConfirmMatchResultSubmission(
        match_id=no_pending.match_id,
        submission_id=902,
        expected_state_fingerprint=no_pending.state_fingerprint,
        expected_candidate_fingerprint=_candidate().fingerprint,
        actor_discord_user_id="1",
        guild_id="2",
        idempotency_key="no-pending",
    )
    with pytest.raises(MatchResultConfirmationUnavailableError):
        _commands(RecordingRepository(no_pending))[0].confirm(no_pending_command)


def test_exact_retry_uses_committed_receipt_and_changed_payload_conflicts() -> None:
    target = _target()
    command = _command(target)
    committed = _commands(RecordingRepository(target))[0].confirm(command)
    stored = StoredMatchResultConfirmationOperation(
        request_fingerprint=command.request_fingerprint,
        type=MATCH_RESULT_CONFIRMED_AUDIT_TYPE,
        match_id=target.match_id,
        after_data=committed.to_audit_payload(),
    )
    post_confirm = _target(
        status=MatchStatus.RESULT_CONFIRMED,
        pending=False,
        confirmed=True,
    )
    repository = RecordingRepository(post_confirm, stored=stored)
    commands, _ = _commands(repository)

    assert commands.confirm(command) == committed
    assert repository.calls == ["lock_target", "find_operation"]

    changed = ConfirmMatchResultSubmission(
        match_id=command.match_id,
        submission_id=command.submission_id,
        expected_state_fingerprint=command.expected_state_fingerprint,
        expected_candidate_fingerprint=command.expected_candidate_fingerprint,
        actor_discord_user_id="999",
        guild_id=command.guild_id,
        idempotency_key=command.idempotency_key,
    )
    with pytest.raises(MatchResultConfirmationIdempotencyConflictError):
        commands.confirm(changed)
