"""SQLAlchemy projections for staff authoritative WIN5 result interactions."""

from __future__ import annotations

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from uma_st2.application.win5.staff_result_queries import (
    Win5NormalResultEntryOption,
    Win5NormalResultTargetChoice,
    Win5NormalResultTargetMode,
    Win5NormalResultTargetSource,
    Win5SpecialResultRace,
    Win5SpecialResultReferenceEntry,
    Win5SpecialResultTargetChoice,
    Win5SpecialResultTargetMode,
    Win5SpecialResultTargetSource,
)
from uma_st2.domain.win5 import (
    Win5NormalResultPlacement,
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SpecialResultWinner,
)

from .datetime_codec import from_database_utc
from .orm import (
    Win5RaceEntryORM,
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


class SqlAlchemyWin5StaffResultQueryRepository:
    """Build immutable staff result projections from one active Session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_normal_result_target_choices(
        self,
        *,
        mode: Win5NormalResultTargetMode,
        limit: int,
    ) -> tuple[Win5NormalResultTargetChoice, ...]:
        race_count = (
            select(func.count(Win5RaceORM.id))
            .where(Win5RaceORM.round_id == Win5RoundORM.id)
            .correlate(Win5RoundORM)
            .scalar_subquery()
        )
        result_count = (
            select(func.count(Win5ResultORM.id))
            .select_from(Win5RaceORM)
            .join(Win5ResultORM, Win5ResultORM.race_id == Win5RaceORM.id)
            .where(Win5RaceORM.round_id == Win5RoundORM.id)
            .correlate(Win5RoundORM)
            .scalar_subquery()
        )
        required_result_count = 0 if mode == Win5NormalResultTargetMode.ENTRY else 5
        rows = self._session.execute(
            select(Win5SeasonORM, Win5RoundORM, Win5RaceORM)
            .join(Win5RoundORM, Win5RoundORM.season_id == Win5SeasonORM.id)
            .join(Win5RaceORM, Win5RaceORM.round_id == Win5RoundORM.id)
            .where(
                Win5SeasonORM.status == Win5SeasonStatus.ACTIVE.value,
                Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
                Win5RoundORM.type == Win5RoundType.NORMAL.value,
                Win5RoundORM.status == Win5RoundStatus.CLOSED.value,
                race_count == 1,
                result_count == required_result_count,
                ~exists().where(Win5ScoreEventORM.round_id == Win5RoundORM.id),
            )
            .order_by(Win5RoundORM.created_at, Win5RoundORM.id)
            .limit(limit)
        ).all()
        return tuple(
            Win5NormalResultTargetChoice(
                season_id=season.id,
                season_name=season.name,
                round_id=round_.id,
                round_name=round_.name,
                race_id=race.id,
                race_name=race.name,
                mode=mode,
            )
            for season, round_, race in rows
        )

    def get_normal_result_target_source(
        self,
        *,
        round_id: int,
    ) -> Win5NormalResultTargetSource | None:
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
        races = tuple(
            self._session.scalars(select(Win5RaceORM).where(Win5RaceORM.round_id == round_id).order_by(Win5RaceORM.id))
        )
        if len(races) != 1:
            raise ValueError("Normal result target must own exactly one Race.")
        race = races[0]
        entries = tuple(
            Win5NormalResultEntryOption(
                id=entry.id,
                gate_number=entry.gate_number,
                name=entry.name,
            )
            for entry in self._session.scalars(
                select(Win5RaceEntryORM)
                .where(Win5RaceEntryORM.race_id == race.id)
                .order_by(Win5RaceEntryORM.gate_number, Win5RaceEntryORM.id)
            )
        )
        placements: list[Win5NormalResultPlacement] = []
        for result in self._session.scalars(
            select(Win5ResultORM)
            .where(Win5ResultORM.race_id == race.id)
            .order_by(Win5ResultORM.position, Win5ResultORM.id)
        ):
            if result.race_entry_id is None or result.gate_number is not None:
                raise ValueError("Normal result target contains a non-Normal Result row.")
            placements.append(
                Win5NormalResultPlacement(
                    id=result.id,
                    position=result.position,
                    race_entry_id=result.race_entry_id,
                )
            )
        has_score_events = (
            self._session.scalar(select(Win5ScoreEventORM.id).where(Win5ScoreEventORM.round_id == round_id).limit(1))
            is not None
        )
        return Win5NormalResultTargetSource(
            season_id=season.id,
            season_name=season.name,
            season_status=Win5SeasonStatus(season.status),
            round_id=round_.id,
            round_name=round_.name,
            round_type=Win5RoundType(round_.type),
            round_status=Win5RoundStatus(round_.status),
            race_id=race.id,
            race_name=race.name,
            entries=entries,
            placements=tuple(placements),
            has_score_events=has_score_events,
        )

    def list_special_result_target_choices(
        self,
        *,
        mode: Win5SpecialResultTargetMode,
        limit: int,
    ) -> tuple[Win5SpecialResultTargetChoice, ...]:
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
        non_void_race_count = race_count - void_count
        result_count = (
            select(func.count(Win5ResultORM.id))
            .select_from(Win5RaceORM)
            .join(Win5ResultORM, Win5ResultORM.race_id == Win5RaceORM.id)
            .where(Win5RaceORM.round_id == Win5RoundORM.id)
            .correlate(Win5RoundORM)
            .scalar_subquery()
        )
        invalid_result_exists = (
            select(Win5ResultORM.id)
            .join(Win5RaceORM, Win5RaceORM.id == Win5ResultORM.race_id)
            .where(
                Win5RaceORM.round_id == Win5RoundORM.id,
                or_(
                    Win5ResultORM.position != 1,
                    Win5ResultORM.race_entry_id.is_not(None),
                    Win5ResultORM.gate_number.is_(None),
                    Win5ResultORM.gate_number <= 0,
                ),
            )
            .correlate(Win5RoundORM)
            .exists()
        )
        result_on_void_race_exists = (
            select(Win5RaceORM.id)
            .join(Win5ResultORM, Win5ResultORM.race_id == Win5RaceORM.id)
            .where(
                Win5RaceORM.round_id == Win5RoundORM.id,
                Win5RaceORM.voided_at.is_not(None),
            )
            .correlate(Win5RoundORM)
            .exists()
        )
        result_filter = result_count == 0
        if mode == Win5SpecialResultTargetMode.CORRECTION:
            result_filter = (result_count == non_void_race_count) & ~invalid_result_exists & ~result_on_void_race_exists
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
                non_void_race_count > 0,
                result_filter,
                ~exists().where(Win5ScoreEventORM.round_id == Win5RoundORM.id),
            )
            .order_by(Win5RoundORM.created_at, Win5RoundORM.id)
            .limit(limit)
        ).all()
        return tuple(
            Win5SpecialResultTargetChoice(
                season_id=season.id,
                season_name=season.name,
                round_id=round_.id,
                round_name=round_.name,
                race_count=stored_race_count,
                mode=mode,
                void_count=stored_void_count,
            )
            for season, round_, stored_race_count, stored_void_count in rows
        )

    def get_special_result_target_source(
        self,
        *,
        round_id: int,
    ) -> Win5SpecialResultTargetSource | None:
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
        race_rows = tuple(
            self._session.scalars(select(Win5RaceORM).where(Win5RaceORM.round_id == round_id).order_by(Win5RaceORM.id))
        )
        race_ids = tuple(race.id for race in race_rows)
        entries_by_race: dict[int, list[Win5SpecialResultReferenceEntry]] = {race_id: [] for race_id in race_ids}
        if race_ids:
            for entry in self._session.scalars(
                select(Win5RaceEntryORM)
                .where(Win5RaceEntryORM.race_id.in_(race_ids))
                .order_by(
                    Win5RaceEntryORM.race_id,
                    Win5RaceEntryORM.gate_number,
                    Win5RaceEntryORM.id,
                )
            ):
                entries_by_race[entry.race_id].append(
                    Win5SpecialResultReferenceEntry(
                        id=entry.id,
                        gate_number=entry.gate_number,
                        name=entry.name,
                    )
                )
        races = tuple(
            Win5SpecialResultRace(
                id=race.id,
                name=race.name,
                entries=tuple(entries_by_race[race.id]),
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
            for race in race_rows
        )

        winners: list[Win5SpecialResultWinner] = []
        if race_ids:
            results = self._session.scalars(
                select(Win5ResultORM)
                .where(Win5ResultORM.race_id.in_(race_ids))
                .order_by(Win5ResultORM.race_id, Win5ResultORM.id)
            )
            for result in results:
                if result.position != 1 or result.race_entry_id is not None or result.gate_number is None:
                    raise ValueError("Special result target contains a non-Special Result row.")
                winners.append(
                    Win5SpecialResultWinner(
                        id=result.id,
                        race_id=result.race_id,
                        gate_number=result.gate_number,
                    )
                )
        has_score_events = (
            self._session.scalar(select(Win5ScoreEventORM.id).where(Win5ScoreEventORM.round_id == round_id).limit(1))
            is not None
        )
        return Win5SpecialResultTargetSource(
            season_id=season.id,
            season_name=season.name,
            season_status=Win5SeasonStatus(season.status),
            round_id=round_.id,
            round_name=round_.name,
            round_type=Win5RoundType(round_.type),
            round_status=Win5RoundStatus(round_.status),
            races=races,
            winners=tuple(winners),
            has_score_events=has_score_events,
        )


class SqlAlchemyWin5StaffResultQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW exposing its staff result repository."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._win5_staff_result_queries: SqlAlchemyWin5StaffResultQueryRepository | None = None

    @property
    def win5_staff_result_queries(self) -> SqlAlchemyWin5StaffResultQueryRepository:
        return self._require_active_repository(self._win5_staff_result_queries)

    def _activate_repositories(self) -> None:
        self._win5_staff_result_queries = SqlAlchemyWin5StaffResultQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._win5_staff_result_queries = None


class SqlAlchemyWin5StaffResultQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyWin5StaffResultQueryUnitOfWork]
):
    """Create one read-only staff result UoW per application query."""

    unit_of_work_type = SqlAlchemyWin5StaffResultQueryUnitOfWork
