"""SQLAlchemy staff projections for Match result submission."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from uma_st2.application.match import (
    MatchResultSubmissionTarget,
    MatchResultSubmissionTargetChoice,
)
from uma_st2.domain.match import MatchSourceKind, MatchStatus

from .match_result_submission_projection import load_match_result_submission_target
from .orm import MatchEntryORM, MatchORM, MatchResultSubmissionORM
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


class SqlAlchemyMatchStaffResultSubmissionQueryRepository:
    """Build bounded eligible targets and exact closed DTOs."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def search_targets(
        self,
        *,
        search: str,
        limit: int,
    ) -> tuple[MatchResultSubmissionTargetChoice, ...]:
        entry_count = (
            select(func.count(MatchEntryORM.id))
            .where(MatchEntryORM.match_id == MatchORM.id)
            .correlate(MatchORM)
            .scalar_subquery()
        )
        max_revision = (
            select(func.coalesce(func.max(MatchResultSubmissionORM.revision_number), 0))
            .where(MatchResultSubmissionORM.match_id == MatchORM.id)
            .correlate(MatchORM)
            .scalar_subquery()
        )
        statement = select(
            MatchORM.id,
            MatchORM.name,
            MatchORM.status,
            entry_count.label("entry_count"),
            (max_revision + 1).label("next_revision_number"),
        ).where(
            MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
            MatchORM.status.in_(
                (
                    MatchStatus.BETTING_CLOSED.value,
                    MatchStatus.RESULT_CONFIRMED.value,
                )
            ),
            entry_count > 0,
        )
        if search:
            statement = statement.where(MatchORM.name.contains(search))
        rows = self._session.execute(statement.order_by(MatchORM.scheduled_at, MatchORM.id).limit(limit))
        return tuple(
            MatchResultSubmissionTargetChoice(
                match_id=row.id,
                match_name=row.name,
                status=row.status,
                entry_count=row.entry_count,
                next_revision_number=row.next_revision_number,
            )
            for row in rows
        )

    def get_target(self, *, match_id: int) -> MatchResultSubmissionTarget | None:
        target = load_match_result_submission_target(
            self._session,
            match_id=match_id,
            lock=False,
        )
        if target is None:
            return None
        if target.source_kind != MatchSourceKind.NATIVE_V2 or target.status not in {
            MatchStatus.BETTING_CLOSED,
            MatchStatus.RESULT_CONFIRMED,
        }:
            return None
        return target


class SqlAlchemyMatchStaffResultSubmissionQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW for result submission projections."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._queries: SqlAlchemyMatchStaffResultSubmissionQueryRepository | None = None

    @property
    def match_staff_result_submission_queries(
        self,
    ) -> SqlAlchemyMatchStaffResultSubmissionQueryRepository:
        return self._require_active_repository(self._queries)

    def _activate_repositories(self) -> None:
        self._queries = SqlAlchemyMatchStaffResultSubmissionQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._queries = None


class SqlAlchemyMatchStaffResultSubmissionQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchStaffResultSubmissionQueryUnitOfWork]
):
    """Create one read-only result submission query UoW per operation."""

    unit_of_work_type = SqlAlchemyMatchStaffResultSubmissionQueryUnitOfWork
