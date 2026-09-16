"""SQLAlchemy projections for staff Special WIN5 Race-void interactions."""

from __future__ import annotations

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from uma_st2.application.win5.staff_special_void_queries import (
    Win5StaffSpecialVoidRace,
    Win5StaffSpecialVoidTargetChoice,
    Win5StaffSpecialVoidTargetSource,
)
from uma_st2.domain.win5 import Win5RoundSourceKind, Win5RoundStatus, Win5RoundType, Win5SeasonStatus

from .datetime_codec import from_database_utc
from .orm import (
    Win5RaceORM,
    Win5ResultORM,
    Win5RoundORM,
    Win5ScoreEventORM,
    Win5SeasonORM,
)
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


class SqlAlchemyWin5StaffSpecialVoidQueryRepository:
    """Build immutable Special void projections from one active Session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_target_choices(self, *, limit: int) -> tuple[Win5StaffSpecialVoidTargetChoice, ...]:
        race_count = (
            select(func.count(Win5RaceORM.id))
            .where(Win5RaceORM.round_id == Win5RoundORM.id)
            .correlate(Win5RoundORM)
            .scalar_subquery()
        )
        void_count = (
            select(func.count(Win5RaceORM.id))
            .where(
                Win5RaceORM.round_id == Win5RoundORM.id,
                Win5RaceORM.voided_at.is_not(None),
            )
            .correlate(Win5RoundORM)
            .scalar_subquery()
        )
        rows = self._session.execute(
            select(
                Win5SeasonORM,
                Win5RoundORM,
                race_count.label("race_count"),
                void_count.label("void_count"),
            )
            .join(Win5RoundORM, Win5RoundORM.season_id == Win5SeasonORM.id)
            .where(
                Win5SeasonORM.status == Win5SeasonStatus.ACTIVE.value,
                Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
                Win5RoundORM.type == Win5RoundType.SPECIAL.value,
                Win5RoundORM.status == Win5RoundStatus.CLOSED.value,
                race_count > 0,
                ~exists().where(Win5ScoreEventORM.round_id == Win5RoundORM.id),
            )
            .order_by(Win5RoundORM.created_at, Win5RoundORM.id)
            .limit(limit)
        ).all()
        return tuple(
            Win5StaffSpecialVoidTargetChoice(
                season_id=season.id,
                season_name=season.name,
                round_id=round_.id,
                round_name=round_.name,
                race_count=stored_race_count,
                void_count=stored_void_count,
            )
            for season, round_, stored_race_count, stored_void_count in rows
        )

    def get_target_source(self, *, round_id: int) -> Win5StaffSpecialVoidTargetSource | None:
        row = self._session.execute(
            select(Win5SeasonORM, Win5RoundORM)
            .join(Win5RoundORM, Win5RoundORM.season_id == Win5SeasonORM.id)
            .where(
                Win5RoundORM.id == round_id,
                Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
            )
        ).one_or_none()
        if row is None:
            return None
        season, round_ = row
        races: list[Win5StaffSpecialVoidRace] = []
        for race in self._session.scalars(
            select(Win5RaceORM).where(Win5RaceORM.round_id == round_id).order_by(Win5RaceORM.id)
        ):
            if (race.void_reason is None) != (race.voided_at is None):
                raise ValueError("Special Race has an incomplete current void fact.")
            races.append(
                Win5StaffSpecialVoidRace(
                    id=race.id,
                    name=race.name,
                    void_reason=race.void_reason,
                    voided_at=(
                        None
                        if race.voided_at is None
                        else from_database_utc(
                            race.voided_at,
                            field_name="win5_races.voided_at",
                        )
                    ),
                )
            )
        result_count = self._session.scalar(
            select(func.count(Win5ResultORM.id))
            .join(Win5RaceORM, Win5RaceORM.id == Win5ResultORM.race_id)
            .where(Win5RaceORM.round_id == round_id)
        )
        has_score_events = (
            self._session.scalar(select(Win5ScoreEventORM.id).where(Win5ScoreEventORM.round_id == round_id).limit(1))
            is not None
        )
        return Win5StaffSpecialVoidTargetSource(
            season_id=season.id,
            season_name=season.name,
            season_status=Win5SeasonStatus(season.status),
            round_id=round_.id,
            round_name=round_.name,
            round_type=Win5RoundType(round_.type),
            round_status=Win5RoundStatus(round_.status),
            result_count=0 if result_count is None else result_count,
            has_score_events=has_score_events,
            races=tuple(races),
        )


class SqlAlchemyWin5StaffSpecialVoidQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW exposing Special void target persistence."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._win5_staff_special_void_queries: SqlAlchemyWin5StaffSpecialVoidQueryRepository | None = None

    @property
    def win5_staff_special_void_queries(self) -> SqlAlchemyWin5StaffSpecialVoidQueryRepository:
        return self._require_active_repository(self._win5_staff_special_void_queries)

    def _activate_repositories(self) -> None:
        self._win5_staff_special_void_queries = SqlAlchemyWin5StaffSpecialVoidQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._win5_staff_special_void_queries = None


class SqlAlchemyWin5StaffSpecialVoidQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyWin5StaffSpecialVoidQueryUnitOfWork]
):
    """Create one Special void query UoW per Application read."""

    unit_of_work_type = SqlAlchemyWin5StaffSpecialVoidQueryUnitOfWork
