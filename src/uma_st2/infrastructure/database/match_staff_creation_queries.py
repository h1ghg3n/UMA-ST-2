"""SQLAlchemy projections for staff native Match creation."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from uma_st2.application.match import MatchCourseChoice
from uma_st2.domain.match import MatchDirection, MatchSurface, StadiumCourseLayout

from .orm import StadiumCourseORM, StadiumORM
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


class SqlAlchemyMatchStaffCreationQueryRepository:
    """Build immutable course choices from one active read Session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_course_choices(self, *, limit: int) -> tuple[MatchCourseChoice, ...]:
        display_name = func.coalesce(StadiumORM.name_ko, StadiumORM.name_jp)
        rows = self._session.execute(
            select(StadiumCourseORM, display_name.label("stadium_name"))
            .join(StadiumORM, StadiumORM.id == StadiumCourseORM.stadium_id)
            .order_by(
                display_name,
                StadiumCourseORM.surface,
                StadiumCourseORM.distance,
                StadiumCourseORM.direction,
                StadiumCourseORM.layout,
                StadiumCourseORM.id,
            )
            .limit(limit)
        ).all()
        return tuple(
            MatchCourseChoice(
                id=course.id,
                stadium_id=course.stadium_id,
                stadium_name=stadium_name,
                surface=MatchSurface(course.surface),
                distance=course.distance,
                direction=MatchDirection(course.direction),
                layout=StadiumCourseLayout(course.layout),
            )
            for course, stadium_name in rows
        )


class SqlAlchemyMatchStaffCreationQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW exposing Match-creation course choices."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._queries: SqlAlchemyMatchStaffCreationQueryRepository | None = None

    @property
    def match_staff_creation_queries(self) -> SqlAlchemyMatchStaffCreationQueryRepository:
        return self._require_active_repository(self._queries)

    def _activate_repositories(self) -> None:
        self._queries = SqlAlchemyMatchStaffCreationQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._queries = None


class SqlAlchemyMatchStaffCreationQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchStaffCreationQueryUnitOfWork]
):
    """Create one read-only Match-creation UoW per application query."""

    unit_of_work_type = SqlAlchemyMatchStaffCreationQueryUnitOfWork
