"""SQLAlchemy projections for staff Match condition setting."""

from __future__ import annotations

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from uma_st2.application.match import MatchConditionRecord, MatchConditionTarget, MatchConditionValues
from uma_st2.domain.match import MatchSourceKind, MatchStatus

from .datetime_codec import from_database_utc
from .orm import MatchConditionORM, MatchOperationORM, MatchORM
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)

_ELIGIBLE_STATUSES = (MatchStatus.SCHEDULED.value, MatchStatus.ENTRY_CONFIRMED.value)
_CONDITION_OPERATION_TYPES = ("match_conditions_set", "match_conditions_changed")


class SqlAlchemyMatchStaffConditionQueryRepository:
    """Build immutable eligible Match condition targets from one read Session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchConditionTarget, ...]:
        statement = self._base_statement()
        if search:
            filters = [MatchORM.name.contains(search, autoescape=True)]
            if search.isdigit():
                filters.append(MatchORM.id == int(search))
            statement = statement.where(or_(*filters))
        rows = self._session.execute(
            statement.order_by(MatchORM.scheduled_at.desc(), MatchORM.id.desc()).limit(limit)
        ).all()
        return tuple(self._target(*row) for row in rows)

    def get_target(self, *, match_id: int) -> MatchConditionTarget | None:
        row = self._session.execute(self._base_statement().where(MatchORM.id == match_id)).one_or_none()
        return None if row is None else self._target(*row)

    @staticmethod
    def _base_statement() -> Select[tuple[MatchORM, MatchConditionORM | None, int | None]]:
        condition_version = (
            select(func.max(MatchOperationORM.operation_id))
            .where(
                MatchOperationORM.match_id == MatchORM.id,
                MatchOperationORM.type.in_(_CONDITION_OPERATION_TYPES),
            )
            .correlate(MatchORM)
            .scalar_subquery()
        )
        return (
            select(MatchORM, MatchConditionORM, condition_version)
            .outerjoin(MatchConditionORM, MatchConditionORM.match_id == MatchORM.id)
            .where(
                MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
                MatchORM.status.in_(_ELIGIBLE_STATUSES),
            )
        )

    @staticmethod
    def _target(
        match: MatchORM,
        condition: MatchConditionORM | None,
        condition_version: int | None,
    ) -> MatchConditionTarget:
        record = None
        if condition is not None:
            record = MatchConditionRecord(
                values=MatchConditionValues(
                    season=condition.season,
                    weather=condition.weather,
                    time_of_day=condition.time_of_day,
                    track_condition=condition.track_condition,
                ),
                created_at=from_database_utc(condition.created_at, field_name="condition.created_at"),
                updated_at=from_database_utc(condition.updated_at, field_name="condition.updated_at"),
            )
        return MatchConditionTarget(
            match_id=match.id,
            name=match.name,
            source_kind=MatchSourceKind(match.source_kind),
            status=MatchStatus(match.status),
            scheduled_at=from_database_utc(match.scheduled_at, field_name="scheduled_at"),
            condition=record,
            condition_version=condition_version,
        )


class SqlAlchemyMatchStaffConditionQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW exposing eligible Match condition targets."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._queries: SqlAlchemyMatchStaffConditionQueryRepository | None = None

    @property
    def match_staff_condition_queries(self) -> SqlAlchemyMatchStaffConditionQueryRepository:
        return self._require_active_repository(self._queries)

    def _activate_repositories(self) -> None:
        self._queries = SqlAlchemyMatchStaffConditionQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._queries = None


class SqlAlchemyMatchStaffConditionQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchStaffConditionQueryUnitOfWork]
):
    """Create one read-only Match condition query UoW per application query."""

    unit_of_work_type = SqlAlchemyMatchStaffConditionQueryUnitOfWork
