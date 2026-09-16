"""SQLAlchemy staff query projections for native Match betting-close."""

from __future__ import annotations

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from uma_st2.application.match.staff_betting_close_queries import (
    MatchBettingClosePreviewTarget,
    MatchBettingCloseTargetChoice,
)
from uma_st2.domain.match import MatchGrade, MatchSourceKind, MatchStatus

from .datetime_codec import from_database_utc
from .orm import BetORM, MatchEntryORM, MatchORM
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


def _integer_aggregate(value: object, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer aggregate.")
    try:
        converted = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be an integer aggregate.") from exc
    if converted != value:
        raise ValueError(f"{field_name} must be an integer aggregate.")
    return converted


class SqlAlchemyMatchStaffBettingCloseQueryRepository:
    """Build bounded close target choices and closed Preview DTOs."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchBettingCloseTargetChoice, ...]:
        entry_count = (
            select(func.count(MatchEntryORM.id))
            .where(MatchEntryORM.match_id == MatchORM.id)
            .correlate(MatchORM)
            .scalar_subquery()
        )
        active_bet_count = (
            select(func.count(BetORM.id))
            .where(
                BetORM.match_id == MatchORM.id,
                BetORM.status == "active",
                BetORM.active_marker.is_(True),
            )
            .correlate(MatchORM)
            .scalar_subquery()
        )
        statement = select(
            MatchORM.id,
            MatchORM.name,
            entry_count.label("entry_count"),
            active_bet_count.label("active_bet_count"),
        ).where(
            MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
            MatchORM.status == MatchStatus.BETTING_OPEN.value,
        )
        if search:
            statement = statement.where(MatchORM.name.contains(search))
        rows = self._session.execute(statement.order_by(MatchORM.scheduled_at, MatchORM.id).limit(limit))
        return tuple(
            MatchBettingCloseTargetChoice(
                match_id=row.id,
                match_name=row.name,
                entry_count=row.entry_count,
                active_bet_count=row.active_bet_count,
            )
            for row in rows
        )

    def get_target(self, *, match_id: int) -> MatchBettingClosePreviewTarget | None:
        inconsistent_marker = self._session.scalar(
            select(BetORM.id)
            .where(
                BetORM.match_id == match_id,
                or_(
                    and_(BetORM.status == "active", BetORM.active_marker.is_not(True)),
                    and_(BetORM.status != "active", BetORM.active_marker.is_(True)),
                ),
            )
            .limit(1)
        )
        if inconsistent_marker is not None:
            raise ValueError("Bet status and active marker are inconsistent.")
        row = self._session.execute(
            select(
                MatchORM,
                select(func.count(MatchEntryORM.id))
                .where(MatchEntryORM.match_id == match_id)
                .scalar_subquery()
                .label("entry_count"),
                select(func.count(BetORM.id))
                .where(
                    BetORM.match_id == match_id,
                    BetORM.status == "active",
                    BetORM.active_marker.is_(True),
                )
                .scalar_subquery()
                .label("active_bet_count"),
                select(func.coalesce(func.sum(BetORM.amount), 0))
                .where(
                    BetORM.match_id == match_id,
                    BetORM.status == "active",
                    BetORM.active_marker.is_(True),
                )
                .scalar_subquery()
                .label("active_stake_total"),
            ).where(MatchORM.id == match_id)
        ).one_or_none()
        if row is None:
            return None
        return MatchBettingClosePreviewTarget(
            match_id=row.MatchORM.id,
            match_name=row.MatchORM.name,
            source_kind=MatchSourceKind(row.MatchORM.source_kind),
            status=MatchStatus(row.MatchORM.status),
            grade=MatchGrade(row.MatchORM.grade),
            scheduled_at=from_database_utc(
                row.MatchORM.scheduled_at,
                field_name="matches.scheduled_at",
            ),
            entry_count=_integer_aggregate(row.entry_count, field_name="entry_count"),
            active_bet_count=_integer_aggregate(row.active_bet_count, field_name="active_bet_count"),
            active_stake_total=_integer_aggregate(row.active_stake_total, field_name="active_stake_total"),
        )


class SqlAlchemyMatchStaffBettingCloseQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW for betting-close staff projections."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._queries: SqlAlchemyMatchStaffBettingCloseQueryRepository | None = None

    @property
    def match_staff_betting_close_queries(self) -> SqlAlchemyMatchStaffBettingCloseQueryRepository:
        return self._require_active_repository(self._queries)

    def _activate_repositories(self) -> None:
        self._queries = SqlAlchemyMatchStaffBettingCloseQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._queries = None


class SqlAlchemyMatchStaffBettingCloseQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchStaffBettingCloseQueryUnitOfWork]
):
    """Create one read-only Match betting-close query UoW per operation."""

    unit_of_work_type = SqlAlchemyMatchStaffBettingCloseQueryUnitOfWork
