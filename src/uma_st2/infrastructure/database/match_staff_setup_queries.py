"""SQLAlchemy staff projections for native scheduled Match setup editing."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from uma_st2.application.match.staff_setup_queries import MatchSetupEditorTarget
from uma_st2.domain.match import MatchSourceKind, MatchStatus

from .match_setup import load_match_setup_target
from .orm import MatchEntryORM, MatchORM
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


class SqlAlchemyMatchStaffSetupQueryRepository:
    """Build detached setup targets from one read-only Session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchSetupEditorTarget, ...]:
        statement = select(MatchORM.id).where(
            MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
            MatchORM.status == MatchStatus.SCHEDULED.value,
        )
        if search:
            filters = [MatchORM.name.contains(search, autoescape=True)]
            if search.isdigit():
                filters.append(MatchORM.id == int(search))
            statement = statement.where(or_(*filters))
        ids = tuple(
            self._session.scalars(statement.order_by(MatchORM.scheduled_at.desc(), MatchORM.id.desc()).limit(limit))
        )
        targets = tuple(self._target(match_id) for match_id in ids)
        if any(target is None for target in targets):
            raise ValueError("Match setup target disappeared during one read snapshot.")
        return tuple(target for target in targets if target is not None)

    def get_target(self, *, match_id: int) -> MatchSetupEditorTarget | None:
        projection = self._target(match_id)
        target = None if projection is None else projection.setup
        if target is None or target.source_kind != MatchSourceKind.NATIVE_V2 or target.status != MatchStatus.SCHEDULED:
            return None
        return projection

    def _target(self, match_id: int) -> MatchSetupEditorTarget | None:
        target = load_match_setup_target(self._session, match_id=match_id, lock=False)
        if target is None:
            return None
        entry_count = self._session.scalar(
            select(func.count(MatchEntryORM.id)).where(MatchEntryORM.match_id == match_id)
        )
        return MatchSetupEditorTarget(setup=target, entry_count=entry_count or 0)


class SqlAlchemyMatchStaffSetupQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW exposing staff setup projections."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._queries: SqlAlchemyMatchStaffSetupQueryRepository | None = None

    @property
    def match_staff_setup_queries(self) -> SqlAlchemyMatchStaffSetupQueryRepository:
        return self._require_active_repository(self._queries)

    def _activate_repositories(self) -> None:
        self._queries = SqlAlchemyMatchStaffSetupQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._queries = None


class SqlAlchemyMatchStaffSetupQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchStaffSetupQueryUnitOfWork]
):
    """Create one fresh read-only setup query UoW per operation."""

    unit_of_work_type = SqlAlchemyMatchStaffSetupQueryUnitOfWork
