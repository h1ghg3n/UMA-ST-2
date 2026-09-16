"""SQLAlchemy projections for staff WIN5 Season management interactions."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from uma_st2.application.win5.season_lifecycle import (
    Win5SeasonAction,
    Win5SeasonRoundState,
    Win5SeasonSnapshot,
)
from uma_st2.domain.win5 import Win5RoundStatus, Win5SeasonStatus

from .datetime_codec import from_database_utc
from .orm import Win5RoundORM, Win5SeasonORM
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)
from .win5_season_marker import validate_win5_season_marker


class SqlAlchemyWin5StaffSeasonQueryRepository:
    """Build immutable Season selector and preview projections."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_season_targets(
        self,
        *,
        action: Win5SeasonAction,
        offset: int,
        limit: int,
    ) -> tuple[Win5SeasonSnapshot, ...]:
        counts = self._count_expressions()
        statement = select(Win5SeasonORM, *counts).where(*self._action_predicates(action=action, counts=counts))
        rows = self._session.execute(
            statement.order_by(Win5SeasonORM.created_at, Win5SeasonORM.id).offset(offset).limit(limit)
        ).all()
        return tuple(self._snapshot(season, count_values) for season, *count_values in rows)

    def get_season_target(self, *, season_id: int) -> Win5SeasonSnapshot | None:
        counts = self._count_expressions()
        row = self._session.execute(select(Win5SeasonORM, *counts).where(Win5SeasonORM.id == season_id)).one_or_none()
        if row is None:
            return None
        season, *count_values = row
        return self._snapshot(season, count_values)

    @staticmethod
    def _count_expressions() -> tuple[object, ...]:
        return tuple(
            select(func.count(Win5RoundORM.id))
            .where(
                Win5RoundORM.season_id == Win5SeasonORM.id,
                Win5RoundORM.status == status.value,
            )
            .correlate(Win5SeasonORM)
            .scalar_subquery()
            .label(f"{status.value}_count")
            for status in Win5RoundStatus
        )

    @staticmethod
    def _action_predicates(*, action: Win5SeasonAction, counts: tuple[object, ...]) -> tuple[object, ...]:
        setup, open_, closed, scored, cancelled = counts
        total = setup + open_ + closed + scored + cancelled
        if action == Win5SeasonAction.ACTIVATE:
            return (Win5SeasonORM.status == Win5SeasonStatus.DRAFT.value, total == setup)
        if action == Win5SeasonAction.CLOSE:
            return (
                Win5SeasonORM.status == Win5SeasonStatus.ACTIVE.value,
                total == scored + cancelled,
            )
        if action == Win5SeasonAction.CANCEL:
            return (Win5SeasonORM.status == Win5SeasonStatus.DRAFT.value, total == 0)
        if action == Win5SeasonAction.EDIT:
            return ()
        raise ValueError("Season creation has no target selector.")

    @staticmethod
    def _snapshot(season: Win5SeasonORM, counts: list[int]) -> Win5SeasonSnapshot:
        if len(counts) != len(Win5RoundStatus):
            raise ValueError("Season Round status count projection is incomplete.")
        return Win5SeasonSnapshot(
            id=season.id,
            name=season.name,
            status=validate_win5_season_marker(
                status=season.status,
                active_marker=season.active_marker,
            ),
            starts_at=(
                None if season.starts_at is None else from_database_utc(season.starts_at, field_name="starts_at")
            ),
            ends_at=(None if season.ends_at is None else from_database_utc(season.ends_at, field_name="ends_at")),
            rounds=Win5SeasonRoundState(
                setup=counts[0] or 0,
                open=counts[1] or 0,
                closed=counts[2] or 0,
                scored=counts[3] or 0,
                cancelled=counts[4] or 0,
            ),
        )


class SqlAlchemyWin5StaffSeasonQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW exposing staff Season projections."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._queries: SqlAlchemyWin5StaffSeasonQueryRepository | None = None

    @property
    def win5_staff_season_queries(self) -> SqlAlchemyWin5StaffSeasonQueryRepository:
        return self._require_active_repository(self._queries)

    def _activate_repositories(self) -> None:
        self._queries = SqlAlchemyWin5StaffSeasonQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._queries = None


class SqlAlchemyWin5StaffSeasonQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyWin5StaffSeasonQueryUnitOfWork]
):
    """Create one read-only Season query UoW per application query."""

    unit_of_work_type = SqlAlchemyWin5StaffSeasonQueryUnitOfWork
