"""SQLAlchemy staff projections for native whole-Match cancellation."""

from __future__ import annotations

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from uma_st2.application.match.cancellation import MATCH_CANCELLABLE_STATUSES
from uma_st2.application.match.staff_cancellation_queries import (
    MatchCancellationPreviewTarget,
    MatchCancellationTargetChoice,
)
from uma_st2.domain.betting import BetStatus
from uma_st2.domain.match import MatchGrade, MatchSourceKind, MatchStatus

from .datetime_codec import from_database_utc
from .match_cancellation import _integer_aggregate
from .orm import BetORM, MatchEntryORM, MatchORM
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)

_ELIGIBLE_STATUS_VALUES = tuple(status.value for status in MATCH_CANCELLABLE_STATUSES)


class SqlAlchemyMatchStaffCancellationQueryRepository:
    """Build bounded cancellation choices and aggregate Preview DTOs."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchCancellationTargetChoice, ...]:
        active_bet_count = (
            select(func.count(BetORM.id))
            .where(
                BetORM.match_id == MatchORM.id,
                BetORM.status == BetStatus.ACTIVE.value,
                BetORM.active_marker.is_(True),
            )
            .correlate(MatchORM)
            .scalar_subquery()
        )
        statement = select(
            MatchORM.id,
            MatchORM.name,
            MatchORM.status,
            active_bet_count.label("active_bet_count"),
        ).where(
            MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
            MatchORM.status.in_(_ELIGIBLE_STATUS_VALUES),
            MatchORM.terminal_reason.is_(None),
        )
        if search:
            statement = statement.where(MatchORM.name.contains(search))
        rows = self._session.execute(statement.order_by(MatchORM.scheduled_at, MatchORM.id).limit(limit))
        return tuple(
            MatchCancellationTargetChoice(
                match_id=row.id,
                match_name=row.name,
                status=MatchStatus(row.status),
                active_bet_count=_integer_aggregate(row.active_bet_count, field_name="active_bet_count"),
            )
            for row in rows
        )

    def get_target(self, *, match_id: int) -> MatchCancellationPreviewTarget | None:
        match = self._session.get(MatchORM, match_id)
        if match is None:
            return None
        inconsistent_marker = self._session.scalar(
            select(BetORM.id)
            .where(
                BetORM.match_id == match_id,
                or_(
                    and_(BetORM.status == BetStatus.ACTIVE.value, BetORM.active_marker.is_not(True)),
                    and_(BetORM.status != BetStatus.ACTIVE.value, BetORM.active_marker.is_(True)),
                ),
            )
            .limit(1)
        )
        if inconsistent_marker is not None:
            raise ValueError("Bet status and active marker are inconsistent.")
        settled_bet = self._session.scalar(
            select(BetORM.id).where(BetORM.match_id == match_id, BetORM.status == BetStatus.SETTLED.value).limit(1)
        )
        if settled_bet is not None:
            raise ValueError("A pre-settlement cancellation target contains a settled Bet.")
        entry_count = self._session.scalar(
            select(func.count(MatchEntryORM.id)).where(MatchEntryORM.match_id == match_id)
        )
        active_bet_count, active_stake_total, affected_persona_count = self._session.execute(
            select(
                func.count(BetORM.id),
                func.coalesce(func.sum(BetORM.amount), 0),
                func.count(func.distinct(BetORM.persona_id)),
            ).where(
                BetORM.match_id == match_id,
                BetORM.status == BetStatus.ACTIVE.value,
                BetORM.active_marker.is_(True),
            )
        ).one()
        return MatchCancellationPreviewTarget(
            match_id=match.id,
            match_name=match.name,
            source_kind=MatchSourceKind(match.source_kind),
            status=MatchStatus(match.status),
            terminal_reason=match.terminal_reason,
            grade=MatchGrade(match.grade),
            scheduled_at=from_database_utc(match.scheduled_at, field_name="matches.scheduled_at"),
            entry_count=_integer_aggregate(entry_count, field_name="entry_count"),
            active_bet_count=_integer_aggregate(active_bet_count, field_name="active_bet_count"),
            active_stake_total=_integer_aggregate(active_stake_total, field_name="active_stake_total"),
            affected_persona_count=_integer_aggregate(
                affected_persona_count,
                field_name="affected_persona_count",
            ),
        )


class SqlAlchemyMatchStaffCancellationQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW for Match cancellation projections."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._queries: SqlAlchemyMatchStaffCancellationQueryRepository | None = None

    @property
    def match_staff_cancellation_queries(self) -> SqlAlchemyMatchStaffCancellationQueryRepository:
        return self._require_active_repository(self._queries)

    def _activate_repositories(self) -> None:
        self._queries = SqlAlchemyMatchStaffCancellationQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._queries = None


class SqlAlchemyMatchStaffCancellationQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchStaffCancellationQueryUnitOfWork]
):
    """Create one read-only cancellation query UoW per operation."""

    unit_of_work_type = SqlAlchemyMatchStaffCancellationQueryUnitOfWork
