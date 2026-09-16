"""SQLAlchemy authoritative Match result confirmation persistence."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from uma_st2.application.match import (
    MATCH_RESULT_CONFIRMATION_AUDIT_SCHEMA_VERSION,
    MATCH_RESULT_CONFIRMED_AUDIT_TYPE,
    ConfirmedMatchResultSubmission,
    ConfirmMatchResultSubmission,
    MatchResultSubmissionTarget,
    StoredMatchResultConfirmationOperation,
)
from uma_st2.domain.match import (
    MatchResultSubmissionStatus,
    MatchSourceKind,
    MatchStatus,
)

from .datetime_codec import to_database_utc
from .match_result_submission_projection import load_match_result_submission_target
from .orm import (
    MatchEntryORM,
    MatchOperationORM,
    MatchORM,
    MatchResultSubmissionORM,
    OperationORM,
)
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


class SqlAlchemyMatchResultConfirmationRepository:
    """Promote one pending revision and materialize its complete board atomically."""

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
    ) -> StoredMatchResultConfirmationOperation | None:
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
        return StoredMatchResultConfirmationOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            match_id=row.match_id,
            after_data=row.after_data,
        )

    def persist_confirmation(
        self,
        *,
        command: ConfirmMatchResultSubmission,
        target: MatchResultSubmissionTarget,
        confirmed_at: datetime,
    ) -> ConfirmedMatchResultSubmission:
        pending = target.pending
        if pending is None:
            raise ValueError("Current pending ResultSubmission is unavailable.")
        stored_time = to_database_utc(confirmed_at, field_name="confirmed_at")
        operation = OperationORM(
            guild_id=command.guild_id,
            correlation_id=command.correlation_id,
            actor_discord_user_id=command.actor_discord_user_id,
            idempotency_key=command.idempotency_key,
            request_fingerprint=command.request_fingerprint,
            reason=None,
            created_at=stored_time,
        )
        self._session.add(operation)
        self._session.flush()

        previous_confirmed_id: int | None = None
        if target.confirmed is not None:
            previous_confirmed_id = target.confirmed.submission_id
            superseded = self._session.execute(
                update(MatchResultSubmissionORM)
                .where(
                    MatchResultSubmissionORM.id == previous_confirmed_id,
                    MatchResultSubmissionORM.match_id == target.match_id,
                    MatchResultSubmissionORM.status == MatchResultSubmissionStatus.CONFIRMED.value,
                    MatchResultSubmissionORM.confirmed_marker.is_(True),
                )
                .values(
                    status=MatchResultSubmissionStatus.SUPERSEDED.value,
                    confirmed_marker=None,
                    updated_at=stored_time,
                )
            )
            if superseded.rowcount != 1:
                raise ValueError("Current confirmed ResultSubmission changed before confirmation.")

        promoted = self._session.execute(
            update(MatchResultSubmissionORM)
            .where(
                MatchResultSubmissionORM.id == pending.submission_id,
                MatchResultSubmissionORM.match_id == target.match_id,
                MatchResultSubmissionORM.status == MatchResultSubmissionStatus.PENDING.value,
                MatchResultSubmissionORM.pending_marker.is_(True),
                MatchResultSubmissionORM.confirmed_marker.is_(None),
            )
            .values(
                status=MatchResultSubmissionStatus.CONFIRMED.value,
                pending_marker=None,
                confirmed_marker=True,
                confirmed_operation_id=operation.id,
                confirmed_at=stored_time,
                updated_at=stored_time,
            )
        )
        if promoted.rowcount != 1:
            raise ValueError("Current pending ResultSubmission changed before confirmation.")

        match_changed = self._session.execute(
            update(MatchORM)
            .where(
                MatchORM.id == target.match_id,
                MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
                MatchORM.status == target.status.value,
            )
            .values(
                status=MatchStatus.RESULT_CONFIRMED.value,
                finish_time_ms=pending.candidate.finish_time_ms,
                updated_at=stored_time,
            )
        )
        if match_changed.rowcount != 1:
            raise ValueError("Match changed before result materialization.")

        entry_ids = tuple(entry.entry_id for entry in pending.candidate.entries)
        cleared = self._session.execute(
            update(MatchEntryORM)
            .where(
                MatchEntryORM.match_id == target.match_id,
                MatchEntryORM.id.in_(entry_ids),
            )
            .values(
                rank=None,
                popularity_rank=None,
                margin=None,
                updated_at=stored_time,
            )
        )
        if cleared.rowcount != len(entry_ids):
            raise ValueError("Current Match Entry set changed before result materialization.")

        for candidate_entry in pending.candidate.entries:
            materialized = self._session.execute(
                update(MatchEntryORM)
                .where(
                    MatchEntryORM.id == candidate_entry.entry_id,
                    MatchEntryORM.match_id == target.match_id,
                    MatchEntryORM.entry_number == candidate_entry.entry_number,
                )
                .values(
                    rank=candidate_entry.rank,
                    popularity_rank=candidate_entry.popularity_rank,
                    margin=candidate_entry.margin,
                    updated_at=stored_time,
                )
            )
            if materialized.rowcount != 1:
                raise ValueError("Current Match Entry identity changed before result materialization.")

        confirmed = ConfirmedMatchResultSubmission(
            submission_id=pending.submission_id,
            match_id=target.match_id,
            match_name=target.match_name,
            revision_number=pending.revision_number,
            source_kind=pending.source_kind,
            status=MatchResultSubmissionStatus.CONFIRMED,
            match_status=MatchStatus.RESULT_CONFIRMED,
            candidate=pending.candidate,
            candidate_fingerprint=pending.candidate.fingerprint,
            previous_confirmed_submission_id=previous_confirmed_id,
            confirmed_at=confirmed_at,
        )
        self._session.add(
            MatchOperationORM(
                operation_id=operation.id,
                match_id=target.match_id,
                type=MATCH_RESULT_CONFIRMED_AUDIT_TYPE,
                before_data={
                    "schema_version": MATCH_RESULT_CONFIRMATION_AUDIT_SCHEMA_VERSION,
                    "match_id": target.match_id,
                    "match_name": target.match_name,
                    "match_status": target.status.value,
                    "state_fingerprint": target.state_fingerprint,
                    "pending": pending.state_payload(),
                    "confirmed": (target.confirmed.state_payload() if target.confirmed is not None else None),
                },
                after_data=confirmed.to_audit_payload(),
            )
        )
        self._session.flush()
        return confirmed


class SqlAlchemyMatchResultConfirmationUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete UoW for one authoritative result confirmation."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._repository: SqlAlchemyMatchResultConfirmationRepository | None = None

    @property
    def match_result_confirmation(self) -> SqlAlchemyMatchResultConfirmationRepository:
        return self._require_active_repository(self._repository)

    def _activate_repositories(self) -> None:
        self._repository = SqlAlchemyMatchResultConfirmationRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._repository = None


class SqlAlchemyMatchResultConfirmationUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchResultConfirmationUnitOfWork]
):
    """Create one fresh authoritative result confirmation UoW per command."""

    unit_of_work_type = SqlAlchemyMatchResultConfirmationUnitOfWork
