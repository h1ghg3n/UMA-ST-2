"""SQLAlchemy projections for staff WIN5 Round creation interactions."""

from __future__ import annotations

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from uma_st2.application.win5.staff_round_creation_queries import Win5RoundCreationSeasonChoice
from uma_st2.domain.win5 import Win5SeasonStatus

from .datetime_codec import from_database_utc
from .orm import Win5SeasonORM
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


class SqlAlchemyWin5StaffRoundCreationQueryRepository:
    """Build immutable eligible-Season projections from one active Session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_round_creation_seasons(
        self,
        *,
        offset: int,
        limit: int,
    ) -> tuple[Win5RoundCreationSeasonChoice, ...]:
        rows = self._session.scalars(
            select(Win5SeasonORM)
            .where(
                Win5SeasonORM.status.in_(
                    (
                        Win5SeasonStatus.DRAFT.value,
                        Win5SeasonStatus.ACTIVE.value,
                    )
                )
            )
            .order_by(
                case((Win5SeasonORM.status == Win5SeasonStatus.ACTIVE.value, 0), else_=1),
                Win5SeasonORM.created_at.desc(),
                Win5SeasonORM.id.desc(),
            )
            .offset(offset)
            .limit(limit)
        ).all()
        return tuple(
            Win5RoundCreationSeasonChoice(
                id=season.id,
                name=season.name,
                status=Win5SeasonStatus(season.status),
                starts_at=(
                    from_database_utc(
                        season.starts_at,
                        field_name="win5_seasons.starts_at",
                    )
                    if season.starts_at is not None
                    else None
                ),
                ends_at=(
                    from_database_utc(
                        season.ends_at,
                        field_name="win5_seasons.ends_at",
                    )
                    if season.ends_at is not None
                    else None
                ),
            )
            for season in rows
        )


class SqlAlchemyWin5StaffRoundCreationQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW exposing staff Round-creation projections."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._queries: SqlAlchemyWin5StaffRoundCreationQueryRepository | None = None

    @property
    def win5_staff_round_creation_queries(self) -> SqlAlchemyWin5StaffRoundCreationQueryRepository:
        return self._require_active_repository(self._queries)

    def _activate_repositories(self) -> None:
        self._queries = SqlAlchemyWin5StaffRoundCreationQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._queries = None


class SqlAlchemyWin5StaffRoundCreationQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyWin5StaffRoundCreationQueryUnitOfWork]
):
    """Create one read-only Round-creation UoW per application query."""

    unit_of_work_type = SqlAlchemyWin5StaffRoundCreationQueryUnitOfWork
