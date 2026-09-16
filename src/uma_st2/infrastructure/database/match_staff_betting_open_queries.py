"""SQLAlchemy staff query projections for native Match betting-open."""

from __future__ import annotations

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from uma_st2.application.match.betting_open import MatchBettingOpenTarget
from uma_st2.application.match.staff_betting_open_queries import MatchBettingOpenTargetChoice
from uma_st2.domain.match import MatchGrade, MatchSourceKind, MatchStatus

from .match_betting_open import (
    load_match_betting_open_rating_rule_coverages,
    load_match_betting_open_target,
)
from .orm import MatchConditionORM, MatchEntryORM, MatchORM
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


class SqlAlchemyMatchStaffBettingOpenQueryRepository:
    """Build bounded target choices and complete closed Preview DTOs."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchBettingOpenTargetChoice, ...]:
        entry_count = (
            select(func.count(MatchEntryORM.id))
            .where(MatchEntryORM.match_id == MatchORM.id)
            .correlate(MatchORM)
            .scalar_subquery()
        )
        has_condition = exists(select(MatchConditionORM.match_id).where(MatchConditionORM.match_id == MatchORM.id))
        statement = select(
            MatchORM.id,
            MatchORM.name,
            MatchORM.grade,
            entry_count.label("entry_count"),
            has_condition.label("has_condition"),
        ).where(
            MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
            MatchORM.status == MatchStatus.SCHEDULED.value,
        )
        if search:
            statement = statement.where(MatchORM.name.contains(search))
        rows = tuple(self._session.execute(statement.order_by(MatchORM.scheduled_at, MatchORM.id).limit(limit)))
        coverage_by_target = load_match_betting_open_rating_rule_coverages(
            self._session,
            targets=tuple((MatchGrade(row.grade), row.entry_count) for row in rows),
            lock=False,
        )
        return tuple(
            MatchBettingOpenTargetChoice(
                match_id=row.id,
                match_name=row.name,
                grade=MatchGrade(row.grade),
                entry_count=row.entry_count,
                has_complete_condition=row.has_condition,
                rating_rule_coverage=coverage_by_target[(MatchGrade(row.grade), row.entry_count)],
            )
            for row in rows
        )

    def get_target(self, *, match_id: int, guild_id: str) -> MatchBettingOpenTarget | None:
        return load_match_betting_open_target(
            self._session,
            match_id=match_id,
            guild_id=guild_id,
            lock=False,
        )


class SqlAlchemyMatchStaffBettingOpenQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW for betting-open staff projections."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._queries: SqlAlchemyMatchStaffBettingOpenQueryRepository | None = None

    @property
    def match_staff_betting_open_queries(self) -> SqlAlchemyMatchStaffBettingOpenQueryRepository:
        return self._require_active_repository(self._queries)

    def _activate_repositories(self) -> None:
        self._queries = SqlAlchemyMatchStaffBettingOpenQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._queries = None


class SqlAlchemyMatchStaffBettingOpenQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchStaffBettingOpenQueryUnitOfWork]
):
    """Create one read-only Match betting-open query UoW per operation."""

    unit_of_work_type = SqlAlchemyMatchStaffBettingOpenQueryUnitOfWork
