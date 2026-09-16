"""SQLAlchemy persistence for immutable RatingRuleVersion seeds."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from uma_st2.application.rating import SeedRatingRuleVersion, StoredRatingRuleVersion

from .datetime_codec import from_database_utc, to_database_utc
from .orm import RatingRuleORM, RatingRuleVersionORM
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


class SqlAlchemyRatingRuleSeedRepository:
    """Create one immutable workbook-derived rule version in an active Session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find_source_version(self, *, command: SeedRatingRuleVersion) -> StoredRatingRuleVersion | None:
        version = self._session.scalar(
            select(RatingRuleVersionORM)
            .where(
                RatingRuleVersionORM.source_identifier == command.source_identifier,
                RatingRuleVersionORM.source_checksum == command.source_checksum,
                RatingRuleVersionORM.source_sheet_name == command.source_sheet_name,
                RatingRuleVersionORM.source_range == command.source_range,
            )
            .with_for_update()
        )
        return _stored_version(version) if version is not None else None

    def lock_latest_version_number(self) -> int:
        versions = tuple(
            self._session.scalars(
                select(RatingRuleVersionORM).order_by(RatingRuleVersionORM.version_number).with_for_update()
            )
        )
        return versions[-1].version_number if versions else 0

    def create_version(
        self,
        *,
        command: SeedRatingRuleVersion,
        version_number: int,
        created_at: datetime,
    ) -> StoredRatingRuleVersion:
        stored_created_at = to_database_utc(created_at, field_name="created_at")
        version = RatingRuleVersionORM(
            version_number=version_number,
            source_identifier=command.source_identifier,
            source_checksum=command.source_checksum,
            source_sheet_name=command.source_sheet_name,
            source_range=command.source_range,
            rule_set_checksum=command.rule_set_checksum,
            rule_count=len(command.rules),
            created_at=stored_created_at,
        )
        self._session.add(version)
        self._session.flush()
        self._session.add_all(
            RatingRuleORM(
                rating_rule_version_id=version.id,
                grade=rule.grade.value,
                participant_count=rule.participant_count,
                converted_rank=rule.converted_rank,
                base_delta=rule.base_delta,
                created_at=stored_created_at,
            )
            for rule in command.rules
        )
        self._session.flush()
        return _stored_version(version)


def _stored_version(version: RatingRuleVersionORM) -> StoredRatingRuleVersion:
    return StoredRatingRuleVersion(
        version_id=version.id,
        version_number=version.version_number,
        rule_set_checksum=version.rule_set_checksum,
        rule_count=version.rule_count,
        created_at=from_database_utc(version.created_at, field_name="RatingRuleVersion.created_at"),
    )


class SqlAlchemyRatingRuleSeedUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete feature UoW exposing immutable Rating rule seeding."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._rating_rule_seed: SqlAlchemyRatingRuleSeedRepository | None = None

    @property
    def rating_rule_seed(self) -> SqlAlchemyRatingRuleSeedRepository:
        return self._require_active_repository(self._rating_rule_seed)

    def _activate_repositories(self) -> None:
        self._rating_rule_seed = SqlAlchemyRatingRuleSeedRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._rating_rule_seed = None


class SqlAlchemyRatingRuleSeedUnitOfWorkFactory(SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyRatingRuleSeedUnitOfWork]):
    """Create one Rating rule seed UoW per controlled command."""

    unit_of_work_type = SqlAlchemyRatingRuleSeedUnitOfWork
