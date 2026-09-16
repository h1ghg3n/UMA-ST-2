"""SQLAlchemy implementation of native Match result candidate submission."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from uma_st2.application.match import (
    MATCH_RESULT_SUBMISSION_AUDIT_SCHEMA_VERSION,
    MatchResultSubmissionAuditType,
    MatchResultSubmissionTarget,
    SavedMatchResultSubmission,
    SaveMatchResultSubmission,
    StoredMatchResultSubmissionOperation,
)
from uma_st2.domain.match import MatchResultSubmissionStatus

from .datetime_codec import to_database_utc
from .match_result_submission_projection import load_match_result_submission_target
from .orm import (
    MatchOperationORM,
    MatchResultSubmissionORM,
    OperationORM,
)
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


class SqlAlchemyMatchResultSubmissionRepository:
    """Lock result authority and persist one complete pending revision."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_target(self, *, match_id: int) -> MatchResultSubmissionTarget | None:
        return load_match_result_submission_target(
            self._session,
            match_id=match_id,
            lock=True,
        )

    def find_operation(
        self,
        *,
        idempotency_key: str,
    ) -> StoredMatchResultSubmissionOperation | None:
        row = self._session.execute(
            select(
                OperationORM.request_fingerprint,
                MatchOperationORM.type,
                MatchOperationORM.match_id,
                MatchOperationORM.after_data,
            )
            .outerjoin(
                MatchOperationORM,
                MatchOperationORM.operation_id == OperationORM.id,
            )
            .where(OperationORM.idempotency_key == idempotency_key)
            .with_for_update()
        ).one_or_none()
        if row is None:
            return None
        return StoredMatchResultSubmissionOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            match_id=row.match_id,
            after_data=row.after_data,
        )

    def persist_submission(
        self,
        *,
        command: SaveMatchResultSubmission,
        target: MatchResultSubmissionTarget,
        operation_type: MatchResultSubmissionAuditType,
        submitted_at: datetime,
    ) -> SavedMatchResultSubmission:
        stored_time = to_database_utc(submitted_at, field_name="submitted_at")
        operation = OperationORM(
            guild_id=command.guild_id,
            correlation_id=command.correlation_id,
            actor_discord_user_id=command.actor_discord_user_id,
            idempotency_key=command.idempotency_key,
            request_fingerprint=command.request_fingerprint,
            reason=command.reason,
            created_at=stored_time,
        )
        self._session.add(operation)
        self._session.flush()

        superseded_submission_id = None
        if target.pending is not None:
            superseded_submission_id = target.pending.submission_id
            changed = self._session.execute(
                update(MatchResultSubmissionORM)
                .where(
                    MatchResultSubmissionORM.id == target.pending.submission_id,
                    MatchResultSubmissionORM.match_id == target.match_id,
                    MatchResultSubmissionORM.status == MatchResultSubmissionStatus.PENDING.value,
                    MatchResultSubmissionORM.pending_marker.is_(True),
                )
                .values(
                    status=MatchResultSubmissionStatus.SUPERSEDED.value,
                    pending_marker=None,
                    updated_at=stored_time,
                )
            )
            if changed.rowcount != 1:
                raise ValueError("Current pending ResultSubmission changed before supersede.")

        submission = MatchResultSubmissionORM(
            match_id=target.match_id,
            revision_number=target.next_revision_number,
            source_kind=command.source_kind.value,
            status=MatchResultSubmissionStatus.PENDING.value,
            pending_marker=True,
            confirmed_marker=None,
            candidate_json=command.candidate.to_payload(),
            submitted_operation_id=operation.id,
            rejected_operation_id=None,
            rejected_at=None,
            rejection_reason=None,
            confirmed_operation_id=None,
            confirmed_at=None,
            created_at=stored_time,
            updated_at=stored_time,
        )
        self._session.add(submission)
        self._session.flush()

        saved = SavedMatchResultSubmission(
            submission_id=submission.id,
            match_id=target.match_id,
            match_name=target.match_name,
            revision_number=target.next_revision_number,
            source_kind=command.source_kind,
            status=MatchResultSubmissionStatus.PENDING,
            candidate=command.candidate,
            candidate_fingerprint=command.candidate.fingerprint,
            superseded_submission_id=superseded_submission_id,
            corrects_confirmed=target.confirmed is not None,
            submitted_at=submitted_at,
        )
        self._session.add(
            MatchOperationORM(
                operation_id=operation.id,
                match_id=target.match_id,
                type=operation_type.value,
                before_data={
                    "schema_version": MATCH_RESULT_SUBMISSION_AUDIT_SCHEMA_VERSION,
                    "match_id": target.match_id,
                    "match_name": target.match_name,
                    "match_status": target.status.value,
                    "state_fingerprint": target.state_fingerprint,
                    "pending": (target.pending.state_payload() if target.pending is not None else None),
                    "confirmed": (target.confirmed.state_payload() if target.confirmed is not None else None),
                },
                after_data=saved.to_audit_payload(),
            )
        )
        self._session.flush()
        return saved


class SqlAlchemyMatchResultSubmissionUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete UoW for one manual Match result candidate revision."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._repository: SqlAlchemyMatchResultSubmissionRepository | None = None

    @property
    def match_result_submission(self) -> SqlAlchemyMatchResultSubmissionRepository:
        return self._require_active_repository(self._repository)

    def _activate_repositories(self) -> None:
        self._repository = SqlAlchemyMatchResultSubmissionRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._repository = None


class SqlAlchemyMatchResultSubmissionUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchResultSubmissionUnitOfWork]
):
    """Create one fresh result submission UoW per command."""

    unit_of_work_type = SqlAlchemyMatchResultSubmissionUnitOfWork
