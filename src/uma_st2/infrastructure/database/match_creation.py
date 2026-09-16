"""SQLAlchemy implementation of native V2 Circle Match creation."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from uma_st2.application.match import (
    CreateMatch,
    MatchConditionRecord,
    MatchCreationAuditType,
    MatchCreationCourse,
    MatchCreationSnapshot,
    StoredMatchCreationOperation,
)
from uma_st2.domain.match import (
    MatchDirection,
    MatchSourceKind,
    MatchStatus,
    MatchSurface,
    StadiumCourseLayout,
)

from .datetime_codec import to_database_utc
from .orm import (
    MatchConditionORM,
    MatchOperationORM,
    MatchORM,
    OperationORM,
    StadiumCourseORM,
    StadiumORM,
)
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


class SqlAlchemyMatchCreationRepository:
    """Create one Match and its operation audit in one active Session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_course(self, *, stadium_course_id: int) -> MatchCreationCourse | None:
        row = self._session.execute(
            select(StadiumCourseORM, StadiumORM)
            .join(StadiumORM, StadiumORM.id == StadiumCourseORM.stadium_id)
            .where(StadiumCourseORM.id == stadium_course_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            return None
        course, stadium = row
        return MatchCreationCourse(
            id=course.id,
            stadium_id=stadium.id,
            stadium_name=stadium.name_ko or stadium.name_jp,
            surface=MatchSurface(course.surface),
            distance=course.distance,
            direction=MatchDirection(course.direction),
            layout=StadiumCourseLayout(course.layout),
        )

    def find_operation(self, *, idempotency_key: str) -> StoredMatchCreationOperation | None:
        row = self._session.execute(
            select(
                OperationORM.request_fingerprint,
                MatchOperationORM.type,
                MatchOperationORM.match_id,
                MatchOperationORM.after_data,
            )
            .outerjoin(MatchOperationORM, MatchOperationORM.operation_id == OperationORM.id)
            .where(OperationORM.idempotency_key == idempotency_key)
            .with_for_update()
        ).one_or_none()
        if row is None:
            return None
        return StoredMatchCreationOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            match_id=row.match_id,
            after_data=row.after_data,
        )

    def create_match(
        self,
        *,
        command: CreateMatch,
        course: MatchCreationCourse,
        created_at: datetime,
    ) -> MatchCreationSnapshot:
        stored_created_at = to_database_utc(created_at, field_name="created_at")
        match = MatchORM(
            name=command.name,
            description=command.description,
            source_kind=MatchSourceKind.NATIVE_V2.value,
            grade=command.grade.value,
            stadium_course_id=command.stadium_course_id,
            scheduled_at=to_database_utc(command.scheduled_at, field_name="scheduled_at"),
            status=MatchStatus.SCHEDULED.value,
            terminal_reason=None,
            finish_time_ms=None,
            created_at=stored_created_at,
            updated_at=stored_created_at,
        )
        self._session.add(match)
        self._session.flush()
        condition = MatchConditionORM(
            match_id=match.id,
            season=command.condition.season.value,
            weather=command.condition.weather.value,
            time_of_day=command.condition.time_of_day.value,
            track_condition=command.condition.track_condition.value,
            created_at=stored_created_at,
            updated_at=stored_created_at,
        )
        self._session.add(condition)
        self._session.flush()
        return MatchCreationSnapshot(
            match_id=match.id,
            name=command.name,
            description=command.description,
            source_kind=MatchSourceKind.NATIVE_V2,
            grade=command.grade,
            course=course,
            scheduled_at=command.scheduled_at,
            condition=MatchConditionRecord(
                values=command.condition,
                created_at=created_at,
                updated_at=created_at,
            ),
            status=MatchStatus.SCHEDULED,
            created_at=created_at,
        )

    def add_creation_audit(
        self,
        *,
        command: CreateMatch,
        snapshot: MatchCreationSnapshot,
        created_at: datetime,
    ) -> None:
        operation = OperationORM(
            guild_id=command.guild_id,
            correlation_id=command.correlation_id,
            actor_discord_user_id=command.actor_discord_user_id,
            idempotency_key=command.idempotency_key,
            request_fingerprint=command.request_fingerprint,
            reason=command.reason,
            created_at=to_database_utc(created_at, field_name="created_at"),
        )
        self._session.add(operation)
        self._session.flush()
        self._session.add(
            MatchOperationORM(
                operation_id=operation.id,
                match_id=snapshot.match_id,
                type=MatchCreationAuditType.CREATED.value,
                before_data=None,
                after_data=snapshot.to_audit_payload(),
            )
        )
        self._session.flush()


class SqlAlchemyMatchCreationUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete feature UoW exposing native Match creation."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._match_creation: SqlAlchemyMatchCreationRepository | None = None

    @property
    def match_creation(self) -> SqlAlchemyMatchCreationRepository:
        return self._require_active_repository(self._match_creation)

    def _activate_repositories(self) -> None:
        self._match_creation = SqlAlchemyMatchCreationRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._match_creation = None


class SqlAlchemyMatchCreationUnitOfWorkFactory(SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchCreationUnitOfWork]):
    """Create one Match creation UoW per application command."""

    unit_of_work_type = SqlAlchemyMatchCreationUnitOfWork
