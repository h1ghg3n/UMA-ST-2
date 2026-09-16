"""SQLAlchemy projections for staff setup-Round deletion interactions."""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from uma_st2.application.win5.round_deletion import (
    WIN5_ROUND_PUBLICATION_SOURCE_KIND,
    Win5SetupRoundDeletionEntry,
    Win5SetupRoundDeletionRace,
    Win5SetupRoundDeletionSnapshot,
    Win5SetupRoundDependencyState,
)
from uma_st2.application.win5.staff_round_deletion_queries import (
    Win5SetupRoundDeletionTargetChoice,
    Win5SetupRoundDeletionTargetSource,
)
from uma_st2.domain.win5 import Win5RoundSourceKind, Win5RoundStatus, Win5RoundType, Win5SeasonStatus

from .datetime_codec import from_database_utc
from .orm import (
    DiscordPublicationORM,
    Win5RaceEntryORM,
    Win5RaceORM,
    Win5ResultORM,
    Win5RoundORM,
    Win5ScoreEventORM,
    Win5SeasonORM,
    Win5SubmissionORM,
)
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


class SqlAlchemyWin5StaffRoundDeletionQueryRepository:
    """Build immutable deletion choices and graph previews from one Session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_setup_round_deletion_targets(
        self,
        *,
        offset: int,
        limit: int,
    ) -> tuple[Win5SetupRoundDeletionTargetChoice, ...]:
        race_count = (
            select(func.count(Win5RaceORM.id))
            .where(Win5RaceORM.round_id == Win5RoundORM.id)
            .correlate(Win5RoundORM)
            .scalar_subquery()
        )
        entry_count = (
            select(func.count(Win5RaceEntryORM.id))
            .join(Win5RaceORM, Win5RaceORM.id == Win5RaceEntryORM.race_id)
            .where(Win5RaceORM.round_id == Win5RoundORM.id)
            .correlate(Win5RoundORM)
            .scalar_subquery()
        )
        has_submission = (
            select(Win5SubmissionORM.id)
            .where(Win5SubmissionORM.round_id == Win5RoundORM.id)
            .correlate(Win5RoundORM)
            .exists()
        )
        has_result = (
            select(Win5ResultORM.id)
            .join(Win5RaceORM, Win5RaceORM.id == Win5ResultORM.race_id)
            .where(Win5RaceORM.round_id == Win5RoundORM.id)
            .correlate(Win5RoundORM)
            .exists()
        )
        has_score_event = (
            select(Win5ScoreEventORM.id)
            .where(Win5ScoreEventORM.round_id == Win5RoundORM.id)
            .correlate(Win5RoundORM)
            .exists()
        )
        has_publication = (
            select(DiscordPublicationORM.id)
            .where(
                DiscordPublicationORM.source_kind == WIN5_ROUND_PUBLICATION_SOURCE_KIND,
                DiscordPublicationORM.source_id == Win5RoundORM.id,
            )
            .correlate(Win5RoundORM)
            .exists()
        )
        rows = self._session.execute(
            select(
                Win5SeasonORM,
                Win5RoundORM,
                race_count.label("race_count"),
                entry_count.label("entry_count"),
            )
            .join(Win5RoundORM, Win5RoundORM.season_id == Win5SeasonORM.id)
            .where(
                Win5SeasonORM.status.in_(
                    (
                        Win5SeasonStatus.DRAFT.value,
                        Win5SeasonStatus.ACTIVE.value,
                    )
                ),
                Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
                Win5RoundORM.status == Win5RoundStatus.SETUP.value,
                ~has_submission,
                ~has_result,
                ~has_score_event,
                ~has_publication,
            )
            .order_by(
                case((Win5SeasonORM.status == Win5SeasonStatus.ACTIVE.value, 0), else_=1),
                Win5SeasonORM.created_at.desc(),
                Win5SeasonORM.id.desc(),
                Win5RoundORM.created_at.desc(),
                Win5RoundORM.id.desc(),
            )
            .offset(offset)
            .limit(limit)
        ).all()
        return tuple(
            Win5SetupRoundDeletionTargetChoice(
                season_id=season.id,
                season_name=season.name,
                season_status=Win5SeasonStatus(season.status),
                round_id=round_.id,
                round_name=round_.name,
                round_type=Win5RoundType(round_.type),
                round_status=Win5RoundStatus(round_.status),
                race_count=round_race_count or 0,
                entry_count=round_entry_count or 0,
            )
            for season, round_, round_race_count, round_entry_count in rows
        )

    def get_setup_round_deletion_target_source(
        self,
        *,
        round_id: int,
    ) -> Win5SetupRoundDeletionTargetSource | None:
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
        races = self._session.scalars(
            select(Win5RaceORM).where(Win5RaceORM.round_id == round_id).order_by(Win5RaceORM.id)
        ).all()
        race_ids = tuple(race.id for race in races)
        entries = (
            self._session.scalars(
                select(Win5RaceEntryORM)
                .where(Win5RaceEntryORM.race_id.in_(race_ids))
                .order_by(
                    Win5RaceEntryORM.race_id,
                    Win5RaceEntryORM.gate_number,
                    Win5RaceEntryORM.id,
                )
            ).all()
            if race_ids
            else []
        )
        entries_by_race: defaultdict[int, list[Win5SetupRoundDeletionEntry]] = defaultdict(list)
        for entry in entries:
            entries_by_race[entry.race_id].append(
                Win5SetupRoundDeletionEntry(
                    id=entry.id,
                    gate_number=entry.gate_number,
                    name=entry.name,
                )
            )
        snapshot = Win5SetupRoundDeletionSnapshot(
            season_id=season.id,
            season_name=season.name,
            season_status=Win5SeasonStatus(season.status),
            round_id=round_.id,
            round_name=round_.name,
            round_type=Win5RoundType(round_.type),
            round_status=Win5RoundStatus(round_.status),
            source_kind=Win5RoundSourceKind(round_.source_kind),
            opens_at=(
                None
                if round_.opens_at is None
                else from_database_utc(round_.opens_at, field_name="win5_rounds.opens_at")
            ),
            closes_at=(
                None
                if round_.closes_at is None
                else from_database_utc(round_.closes_at, field_name="win5_rounds.closes_at")
            ),
            races=tuple(
                Win5SetupRoundDeletionRace(
                    id=race.id,
                    name=race.name,
                    scheduled_at=(
                        None
                        if race.scheduled_at is None
                        else from_database_utc(race.scheduled_at, field_name="win5_races.scheduled_at")
                    ),
                    entries=tuple(entries_by_race[race.id]),
                )
                for race in races
            ),
        )
        result_count = (
            self._session.scalar(select(func.count(Win5ResultORM.id)).where(Win5ResultORM.race_id.in_(race_ids)))
            if race_ids
            else 0
        )
        dependencies = Win5SetupRoundDependencyState(
            submission_count=(
                self._session.scalar(
                    select(func.count(Win5SubmissionORM.id)).where(Win5SubmissionORM.round_id == round_id)
                )
                or 0
            ),
            result_count=result_count or 0,
            score_event_count=(
                self._session.scalar(
                    select(func.count(Win5ScoreEventORM.id)).where(Win5ScoreEventORM.round_id == round_id)
                )
                or 0
            ),
            publication_count=(
                self._session.scalar(
                    select(func.count(DiscordPublicationORM.id)).where(
                        DiscordPublicationORM.source_kind == WIN5_ROUND_PUBLICATION_SOURCE_KIND,
                        DiscordPublicationORM.source_id == round_id,
                    )
                )
                or 0
            ),
        )
        return Win5SetupRoundDeletionTargetSource(snapshot=snapshot, dependencies=dependencies)


class SqlAlchemyWin5StaffRoundDeletionQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW exposing staff deletion projections."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._queries: SqlAlchemyWin5StaffRoundDeletionQueryRepository | None = None

    @property
    def win5_staff_round_deletion_queries(self) -> SqlAlchemyWin5StaffRoundDeletionQueryRepository:
        return self._require_active_repository(self._queries)

    def _activate_repositories(self) -> None:
        self._queries = SqlAlchemyWin5StaffRoundDeletionQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._queries = None


class SqlAlchemyWin5StaffRoundDeletionQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyWin5StaffRoundDeletionQueryUnitOfWork]
):
    """Create one read-only setup-Round deletion UoW per query."""

    unit_of_work_type = SqlAlchemyWin5StaffRoundDeletionQueryUnitOfWork
