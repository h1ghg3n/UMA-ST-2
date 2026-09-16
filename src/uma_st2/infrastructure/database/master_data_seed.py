"""SQLAlchemy persistence for reviewed initial master-data seeding."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from uma_st2.application.master_data import (
    MasterDataSeedConflictError,
    MasterDataSnapshot,
    SeedMasterData,
    StadiumCourseSeed,
    StadiumSeed,
    UmamusumeSeed,
    UmamusumeVariantSeed,
)
from uma_st2.domain.match import MatchDirection, MatchSurface, StadiumCourseLayout

from .datetime_codec import to_database_utc
from .orm import StadiumCourseORM, StadiumORM, UmamusumeORM, UmamusumeVariantORM
from .uow import SessionFactory, SqlAlchemyFeatureUnitOfWork, SqlAlchemyFeatureUnitOfWorkFactory


class SqlAlchemyMasterDataSeedRepository:
    """Read or create the complete four-table master projection."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_current(self) -> MasterDataSnapshot:
        umamusumes = tuple(
            self._session.scalars(select(UmamusumeORM).order_by(UmamusumeORM.external_id).with_for_update())
        )
        variants = tuple(
            self._session.scalars(
                select(UmamusumeVariantORM).order_by(UmamusumeVariantORM.external_id).with_for_update()
            )
        )
        stadiums = tuple(self._session.scalars(select(StadiumORM).order_by(StadiumORM.external_id).with_for_update()))
        courses = tuple(
            self._session.scalars(select(StadiumCourseORM).order_by(StadiumCourseORM.external_id).with_for_update())
        )
        umamusume_external_ids = {row.id: row.external_id for row in umamusumes}
        stadium_external_ids = {row.id: row.external_id for row in stadiums}
        try:
            return MasterDataSnapshot(
                umamusumes=tuple(
                    UmamusumeSeed(external_id=row.external_id, name_jp=row.name_jp, name_ko=row.name_ko)
                    for row in umamusumes
                ),
                umamusume_variants=tuple(
                    UmamusumeVariantSeed(
                        external_id=row.external_id,
                        umamusume_external_id=umamusume_external_ids[row.umamusume_id],
                        name_jp=row.name_jp,
                        name_ko=row.name_ko,
                        release_date=row.release_date,
                    )
                    for row in variants
                ),
                stadiums=tuple(
                    StadiumSeed(external_id=row.external_id, name_jp=row.name_jp, name_ko=row.name_ko)
                    for row in stadiums
                ),
                stadium_courses=tuple(
                    StadiumCourseSeed(
                        external_id=row.external_id,
                        stadium_external_id=stadium_external_ids[row.stadium_id],
                        surface=MatchSurface(row.surface),
                        distance=row.distance,
                        direction=MatchDirection(row.direction),
                        layout=StadiumCourseLayout(row.layout),
                    )
                    for row in courses
                ),
            )
        except (KeyError, ValueError) as exc:
            raise MasterDataSeedConflictError("Current master data contains an invalid parent or enum value.") from exc

    def create(self, *, command: SeedMasterData, created_at: datetime) -> None:
        stored_at = to_database_utc(created_at, field_name="created_at")
        umamusumes = {
            item.external_id: UmamusumeORM(
                external_id=item.external_id,
                name_jp=item.name_jp,
                name_ko=item.name_ko,
                created_at=stored_at,
                updated_at=stored_at,
            )
            for item in command.umamusumes
        }
        stadiums = {
            item.external_id: StadiumORM(
                external_id=item.external_id,
                name_jp=item.name_jp,
                name_ko=item.name_ko,
                created_at=stored_at,
                updated_at=stored_at,
            )
            for item in command.stadiums
        }
        self._session.add_all(umamusumes.values())
        self._session.add_all(stadiums.values())
        self._session.flush()
        self._session.add_all(
            UmamusumeVariantORM(
                umamusume_id=umamusumes[item.umamusume_external_id].id,
                external_id=item.external_id,
                name_jp=item.name_jp,
                name_ko=item.name_ko,
                release_date=item.release_date,
                created_at=stored_at,
                updated_at=stored_at,
            )
            for item in command.umamusume_variants
        )
        self._session.add_all(
            StadiumCourseORM(
                stadium_id=stadiums[item.stadium_external_id].id,
                external_id=item.external_id,
                surface=item.surface.value,
                distance=item.distance,
                direction=item.direction.value,
                layout=item.layout.value,
                created_at=stored_at,
                updated_at=stored_at,
            )
            for item in command.stadium_courses
        )
        self._session.flush()


class SqlAlchemyMasterDataSeedUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete UoW for one reviewed initial master seed."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._master_data_seed: SqlAlchemyMasterDataSeedRepository | None = None

    @property
    def master_data_seed(self) -> SqlAlchemyMasterDataSeedRepository:
        return self._require_active_repository(self._master_data_seed)

    def _activate_repositories(self) -> None:
        self._master_data_seed = SqlAlchemyMasterDataSeedRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._master_data_seed = None


class SqlAlchemyMasterDataSeedUnitOfWorkFactory(SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMasterDataSeedUnitOfWork]):
    """Create one initial master-data seed UoW per command."""

    unit_of_work_type = SqlAlchemyMasterDataSeedUnitOfWork
