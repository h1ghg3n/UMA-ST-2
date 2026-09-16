"""SQLAlchemy current-pending Match result review and rejection."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from uma_st2.application.match import (
    MATCH_RESULT_REJECTED_AUDIT_TYPE,
    MATCH_RESULT_REJECTION_AUDIT_SCHEMA_VERSION,
    MatchResultCandidate,
    MatchResultReviewTargetChoice,
    MatchResultSubmissionTarget,
    RejectedMatchResultSubmission,
    RejectMatchResultSubmission,
    StoredMatchResultRejectionOperation,
)
from uma_st2.domain.match import (
    MatchResultSubmissionStatus,
    MatchSourceKind,
    MatchStatus,
)

from .datetime_codec import to_database_utc
from .match_result_submission_projection import load_match_result_submission_target
from .orm import (
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


class SqlAlchemyMatchResultReviewQueryRepository:
    """Build bounded current-pending review projections."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def search_targets(
        self,
        *,
        search: str,
        limit: int,
    ) -> tuple[MatchResultReviewTargetChoice, ...]:
        statement = (
            select(
                MatchORM.id,
                MatchORM.name,
                MatchORM.status.label("match_status"),
                MatchResultSubmissionORM.id.label("submission_id"),
                MatchResultSubmissionORM.revision_number,
                MatchResultSubmissionORM.source_kind,
                MatchResultSubmissionORM.candidate_json,
            )
            .join(
                MatchResultSubmissionORM,
                MatchResultSubmissionORM.match_id == MatchORM.id,
            )
            .where(
                MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
                MatchORM.status.in_(
                    (
                        MatchStatus.BETTING_CLOSED.value,
                        MatchStatus.RESULT_CONFIRMED.value,
                    )
                ),
                MatchResultSubmissionORM.status == MatchResultSubmissionStatus.PENDING.value,
                MatchResultSubmissionORM.pending_marker.is_(True),
            )
        )
        if search:
            statement = statement.where(MatchORM.name.contains(search))
        rows = self._session.execute(statement.order_by(MatchORM.scheduled_at, MatchORM.id).limit(limit))
        choices: list[MatchResultReviewTargetChoice] = []
        for row in rows:
            candidate = MatchResultCandidate.from_payload(row.candidate_json)
            choices.append(
                MatchResultReviewTargetChoice(
                    match_id=row.id,
                    match_name=row.name,
                    match_status=row.match_status,
                    submission_id=row.submission_id,
                    revision_number=row.revision_number,
                    source_kind=row.source_kind,
                    entry_count=len(candidate.entries),
                )
            )
        return tuple(choices)

    def get_target(self, *, match_id: int) -> MatchResultSubmissionTarget | None:
        target = load_match_result_submission_target(
            self._session,
            match_id=match_id,
            lock=False,
        )
        if target is None:
            return None
        if (
            target.source_kind != MatchSourceKind.NATIVE_V2
            or target.status
            not in {
                MatchStatus.BETTING_CLOSED,
                MatchStatus.RESULT_CONFIRMED,
            }
            or target.pending is None
        ):
            return None
        return target


class SqlAlchemyMatchResultRejectionRepository:
    """Lock and reject exactly one current pending revision."""

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
    ) -> StoredMatchResultRejectionOperation | None:
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
        return StoredMatchResultRejectionOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            match_id=row.match_id,
            after_data=row.after_data,
        )

    def persist_rejection(
        self,
        *,
        command: RejectMatchResultSubmission,
        target: MatchResultSubmissionTarget,
        rejected_at: datetime,
    ) -> RejectedMatchResultSubmission:
        pending = target.pending
        if pending is None:
            raise ValueError("Current pending ResultSubmission is unavailable.")
        stored_time = to_database_utc(rejected_at, field_name="rejected_at")
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

        changed = self._session.execute(
            update(MatchResultSubmissionORM)
            .where(
                MatchResultSubmissionORM.id == pending.submission_id,
                MatchResultSubmissionORM.match_id == target.match_id,
                MatchResultSubmissionORM.status == MatchResultSubmissionStatus.PENDING.value,
                MatchResultSubmissionORM.pending_marker.is_(True),
            )
            .values(
                status=MatchResultSubmissionStatus.REJECTED.value,
                pending_marker=None,
                rejected_operation_id=operation.id,
                rejected_at=stored_time,
                rejection_reason=command.reason,
                updated_at=stored_time,
            )
        )
        if changed.rowcount != 1:
            raise ValueError("Current pending ResultSubmission changed before rejection.")

        rejected = RejectedMatchResultSubmission(
            submission_id=pending.submission_id,
            match_id=target.match_id,
            match_name=target.match_name,
            revision_number=pending.revision_number,
            source_kind=pending.source_kind,
            status=MatchResultSubmissionStatus.REJECTED,
            candidate_fingerprint=pending.candidate.fingerprint,
            reason=command.reason,
            rejected_at=rejected_at,
            preserved_confirmed_submission_id=(
                target.confirmed.submission_id if target.confirmed is not None else None
            ),
        )
        self._session.add(
            MatchOperationORM(
                operation_id=operation.id,
                match_id=target.match_id,
                type=MATCH_RESULT_REJECTED_AUDIT_TYPE,
                before_data={
                    "schema_version": MATCH_RESULT_REJECTION_AUDIT_SCHEMA_VERSION,
                    "match_id": target.match_id,
                    "match_name": target.match_name,
                    "match_status": target.status.value,
                    "state_fingerprint": target.state_fingerprint,
                    "pending": pending.state_payload(),
                    "confirmed": (target.confirmed.state_payload() if target.confirmed is not None else None),
                },
                after_data=rejected.to_audit_payload(),
            )
        )
        self._session.flush()
        return rejected


class SqlAlchemyMatchResultReviewQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW for one result review query."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._queries: SqlAlchemyMatchResultReviewQueryRepository | None = None

    @property
    def match_result_review_queries(self) -> SqlAlchemyMatchResultReviewQueryRepository:
        return self._require_active_repository(self._queries)

    def _activate_repositories(self) -> None:
        self._queries = SqlAlchemyMatchResultReviewQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._queries = None


class SqlAlchemyMatchResultReviewQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchResultReviewQueryUnitOfWork]
):
    """Create one fresh pending-result review query UoW per operation."""

    unit_of_work_type = SqlAlchemyMatchResultReviewQueryUnitOfWork


class SqlAlchemyMatchResultRejectionUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete UoW for one current-pending rejection."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._repository: SqlAlchemyMatchResultRejectionRepository | None = None

    @property
    def match_result_rejection(self) -> SqlAlchemyMatchResultRejectionRepository:
        return self._require_active_repository(self._repository)

    def _activate_repositories(self) -> None:
        self._repository = SqlAlchemyMatchResultRejectionRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._repository = None


class SqlAlchemyMatchResultRejectionUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchResultRejectionUnitOfWork]
):
    """Create one fresh current-pending rejection UoW per command."""

    unit_of_work_type = SqlAlchemyMatchResultRejectionUnitOfWork
