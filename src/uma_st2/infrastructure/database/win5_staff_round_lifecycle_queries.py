"""SQLAlchemy projections for staff WIN5 Round lifecycle interactions."""

from __future__ import annotations

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from uma_st2.application.win5.round_lifecycle import (
    MAX_OPEN_WIN5_ROUNDS_PER_ACTIVE_SEASON,
    Win5RoundLifecycleAction,
)
from uma_st2.application.win5.staff_round_lifecycle_queries import (
    Win5RoundLifecycleTargetChoice,
    Win5RoundLifecycleTargetPageSource,
    Win5RoundLifecycleTargetSource,
)
from uma_st2.domain.win5 import (
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SubmissionStatus,
)

from .orm import (
    Win5RaceEntryORM,
    Win5RaceORM,
    Win5ResultORM,
    Win5RoundORM,
    Win5SeasonORM,
    Win5SubmissionORM,
)
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


class SqlAlchemyWin5StaffRoundLifecycleQueryRepository:
    """Build immutable Round lifecycle projections from one active Session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_round_lifecycle_target_page_source(
        self,
        *,
        action: Win5RoundLifecycleAction,
        offset: int,
        limit: int,
    ) -> Win5RoundLifecycleTargetPageSource:
        expected_status = Win5RoundStatus.SETUP if action == Win5RoundLifecycleAction.OPEN else Win5RoundStatus.OPEN
        race_count = (
            select(func.count(Win5RaceORM.id))
            .where(Win5RaceORM.round_id == Win5RoundORM.id)
            .correlate(Win5RoundORM)
            .scalar_subquery()
        )
        race_entry_count = (
            select(func.count(Win5RaceEntryORM.id))
            .join(Win5RaceORM, Win5RaceORM.id == Win5RaceEntryORM.race_id)
            .where(Win5RaceORM.round_id == Win5RoundORM.id)
            .correlate(Win5RoundORM)
            .scalar_subquery()
        )
        result_count = (
            select(func.count(Win5ResultORM.id))
            .join(Win5RaceORM, Win5RaceORM.id == Win5ResultORM.race_id)
            .where(Win5RaceORM.round_id == Win5RoundORM.id)
            .correlate(Win5RoundORM)
            .scalar_subquery()
        )
        open_round = aliased(Win5RoundORM)
        season_open_round_count = (
            select(func.count(open_round.id))
            .where(
                open_round.season_id == Win5SeasonORM.id,
                open_round.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
                open_round.status == Win5RoundStatus.OPEN.value,
            )
            .correlate(Win5SeasonORM)
            .scalar_subquery()
        )
        statement = (
            select(
                Win5SeasonORM,
                Win5RoundORM,
                race_count.label("race_count"),
                race_entry_count.label("race_entry_count"),
                result_count.label("result_count"),
                season_open_round_count.label("season_open_round_count"),
            )
            .join(Win5RoundORM, Win5RoundORM.season_id == Win5SeasonORM.id)
            .where(
                Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
                Win5RoundORM.status == expected_status.value,
            )
        )
        if action == Win5RoundLifecycleAction.OPEN:
            statement = statement.where(
                Win5SeasonORM.status == Win5SeasonStatus.ACTIVE.value,
                result_count == 0,
                season_open_round_count < MAX_OPEN_WIN5_ROUNDS_PER_ACTIVE_SEASON,
                or_(
                    and_(
                        Win5RoundORM.type == Win5RoundType.NORMAL.value,
                        race_count == 1,
                        race_entry_count >= 5,
                    ),
                    and_(
                        Win5RoundORM.type == Win5RoundType.SPECIAL.value,
                        race_count >= 1,
                    ),
                ),
            )
        rows = self._session.execute(
            statement.order_by(
                Win5SeasonORM.created_at,
                Win5SeasonORM.id,
                Win5RoundORM.created_at,
                Win5RoundORM.id,
            )
            .offset(offset)
            .limit(limit)
        ).all()
        return Win5RoundLifecycleTargetPageSource(
            choices=tuple(
                Win5RoundLifecycleTargetChoice(
                    season_id=season.id,
                    season_name=season.name,
                    season_status=Win5SeasonStatus(season.status),
                    round_id=round_.id,
                    round_name=round_.name,
                    round_type=Win5RoundType(round_.type),
                    round_status=Win5RoundStatus(round_.status),
                    race_count=race_total or 0,
                    race_entry_count=entry_total or 0,
                    result_count=result_total or 0,
                    season_open_round_count=open_total or 0,
                )
                for season, round_, race_total, entry_total, result_total, open_total in rows
            ),
            has_active_open_overflow=self._has_active_open_overflow(),
        )

    def get_round_lifecycle_target_source(
        self,
        *,
        round_id: int,
    ) -> Win5RoundLifecycleTargetSource | None:
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
        race_ids = select(Win5RaceORM.id).where(Win5RaceORM.round_id == round_id)
        race_count = self._session.scalar(select(func.count(Win5RaceORM.id)).where(Win5RaceORM.round_id == round_id))
        race_entry_count = self._session.scalar(
            select(func.count(Win5RaceEntryORM.id)).where(Win5RaceEntryORM.race_id.in_(race_ids))
        )
        result_count = self._session.scalar(
            select(func.count(Win5ResultORM.id)).where(Win5ResultORM.race_id.in_(race_ids))
        )
        accepted_submission_count = self._session.scalar(
            select(func.count(Win5SubmissionORM.id)).where(
                Win5SubmissionORM.round_id == round_id,
                Win5SubmissionORM.status == Win5SubmissionStatus.ACCEPTED.value,
            )
        )
        season_open_round_count = self._session.scalar(
            select(func.count(Win5RoundORM.id)).where(
                Win5RoundORM.season_id == season.id,
                Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
                Win5RoundORM.status == Win5RoundStatus.OPEN.value,
            )
        )
        return Win5RoundLifecycleTargetSource(
            season_id=season.id,
            season_name=season.name,
            season_status=Win5SeasonStatus(season.status),
            round_id=round_.id,
            round_name=round_.name,
            round_type=Win5RoundType(round_.type),
            round_status=Win5RoundStatus(round_.status),
            race_count=race_count or 0,
            race_entry_count=race_entry_count or 0,
            result_count=result_count or 0,
            accepted_submission_count=accepted_submission_count or 0,
            season_open_round_count=season_open_round_count or 0,
            has_active_open_overflow=self._has_active_open_overflow(),
        )

    def _has_active_open_overflow(self) -> bool:
        overflow_season = (
            select(Win5RoundORM.season_id)
            .join(Win5SeasonORM, Win5SeasonORM.id == Win5RoundORM.season_id)
            .where(
                Win5SeasonORM.status == Win5SeasonStatus.ACTIVE.value,
                Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
                Win5RoundORM.status == Win5RoundStatus.OPEN.value,
            )
            .group_by(Win5RoundORM.season_id)
            .having(func.count(Win5RoundORM.id) > MAX_OPEN_WIN5_ROUNDS_PER_ACTIVE_SEASON)
            .limit(1)
        )
        return self._session.scalar(overflow_season) is not None


class SqlAlchemyWin5StaffRoundLifecycleQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW exposing staff lifecycle projections."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._queries: SqlAlchemyWin5StaffRoundLifecycleQueryRepository | None = None

    @property
    def win5_staff_round_lifecycle_queries(self) -> SqlAlchemyWin5StaffRoundLifecycleQueryRepository:
        return self._require_active_repository(self._queries)

    def _activate_repositories(self) -> None:
        self._queries = SqlAlchemyWin5StaffRoundLifecycleQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._queries = None


class SqlAlchemyWin5StaffRoundLifecycleQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyWin5StaffRoundLifecycleQueryUnitOfWork]
):
    """Create one read-only lifecycle UoW per application query."""

    unit_of_work_type = SqlAlchemyWin5StaffRoundLifecycleQueryUnitOfWork
