"""SQLAlchemy implementation of native scheduled Match setup replacement."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from uma_st2.application.match.conditions import MatchConditionRecord, MatchConditionValues
from uma_st2.application.match.creation import MatchCreationCourse
from uma_st2.application.match.setup import (
    MatchSetupAuditType,
    MatchSetupTarget,
    StoredMatchSetupOperation,
    UpdateMatchSetup,
)
from uma_st2.domain.match import (
    MatchDirection,
    MatchGrade,
    MatchSourceKind,
    MatchStatus,
    MatchSurface,
    StadiumCourseLayout,
)

from .datetime_codec import from_database_utc, to_database_utc
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

_SETUP_OPERATION_TYPES = (
    "match_created",
    "match_conditions_set",
    "match_conditions_changed",
    MatchSetupAuditType.UPDATED.value,
)


def _course(course: StadiumCourseORM, stadium: StadiumORM) -> MatchCreationCourse:
    return MatchCreationCourse(
        id=course.id,
        stadium_id=stadium.id,
        stadium_name=stadium.name_ko or stadium.name_jp,
        surface=MatchSurface(course.surface),
        distance=course.distance,
        direction=MatchDirection(course.direction),
        layout=StadiumCourseLayout(course.layout),
    )


def _condition(condition: MatchConditionORM) -> MatchConditionRecord:
    return MatchConditionRecord(
        values=MatchConditionValues(
            season=condition.season,
            weather=condition.weather,
            time_of_day=condition.time_of_day,
            track_condition=condition.track_condition,
        ),
        created_at=from_database_utc(condition.created_at, field_name="match_conditions.created_at"),
        updated_at=from_database_utc(condition.updated_at, field_name="match_conditions.updated_at"),
    )


def load_match_setup_target(
    session: Session,
    *,
    match_id: int,
    lock: bool,
) -> MatchSetupTarget | None:
    """Load one detached setup while acquiring the Match row first when mutating."""

    match_statement = select(MatchORM).where(MatchORM.id == match_id)
    if lock:
        match_statement = match_statement.with_for_update()
    match = session.scalar(match_statement)
    if match is None:
        return None

    course_statement = (
        select(StadiumCourseORM, StadiumORM)
        .join(StadiumORM, StadiumORM.id == StadiumCourseORM.stadium_id)
        .where(StadiumCourseORM.id == match.stadium_course_id)
    )
    course_row = session.execute(course_statement).one_or_none()
    if course_row is None:
        raise ValueError("Match references a missing stadium course.")

    condition_statement = select(MatchConditionORM).where(MatchConditionORM.match_id == match_id)
    if lock:
        condition_statement = condition_statement.with_for_update()
    condition_row = session.scalar(condition_statement)
    setup_version = session.scalar(
        select(func.max(MatchOperationORM.operation_id)).where(
            MatchOperationORM.match_id == match_id,
            MatchOperationORM.type.in_(_SETUP_OPERATION_TYPES),
        )
    )
    course, stadium = course_row
    return MatchSetupTarget(
        match_id=match.id,
        name=match.name,
        description=match.description,
        source_kind=MatchSourceKind(match.source_kind),
        grade=MatchGrade(match.grade),
        course=_course(course, stadium),
        scheduled_at=from_database_utc(match.scheduled_at, field_name="matches.scheduled_at"),
        status=MatchStatus(match.status),
        condition=None if condition_row is None else _condition(condition_row),
        updated_at=from_database_utc(match.updated_at, field_name="matches.updated_at"),
        setup_version=setup_version,
    )


class SqlAlchemyMatchSetupRepository:
    """Lock, replace, and audit one complete Match setup in one Session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_target(self, *, match_id: int) -> MatchSetupTarget | None:
        return load_match_setup_target(self._session, match_id=match_id, lock=True)

    def lock_course(self, *, stadium_course_id: int) -> MatchCreationCourse | None:
        row = self._session.execute(
            select(StadiumCourseORM, StadiumORM)
            .join(StadiumORM, StadiumORM.id == StadiumCourseORM.stadium_id)
            .where(StadiumCourseORM.id == stadium_course_id)
            .with_for_update()
        ).one_or_none()
        return None if row is None else _course(*row)

    def find_operation(self, *, idempotency_key: str) -> StoredMatchSetupOperation | None:
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
        return StoredMatchSetupOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            match_id=row.match_id,
            after_data=row.after_data,
        )

    def replace_setup(
        self,
        *,
        target: MatchSetupTarget,
        command: UpdateMatchSetup,
        course: MatchCreationCourse,
        changed_at: datetime,
    ) -> MatchSetupTarget:
        match = self._session.get(MatchORM, target.match_id)
        if match is None:
            raise RuntimeError("Match setup replacement lost its locked Match row.")
        stored_changed_at = to_database_utc(changed_at, field_name="changed_at")
        match.name = command.name
        match.description = command.description
        match.grade = command.grade.value
        match.stadium_course_id = course.id
        match.scheduled_at = to_database_utc(command.scheduled_at, field_name="scheduled_at")
        match.updated_at = stored_changed_at

        condition = self._session.get(MatchConditionORM, target.match_id)
        if condition is None:
            condition = MatchConditionORM(
                match_id=target.match_id,
                season=command.condition.season.value,
                weather=command.condition.weather.value,
                time_of_day=command.condition.time_of_day.value,
                track_condition=command.condition.track_condition.value,
                created_at=stored_changed_at,
                updated_at=stored_changed_at,
            )
            self._session.add(condition)
        else:
            condition.season = command.condition.season.value
            condition.weather = command.condition.weather.value
            condition.time_of_day = command.condition.time_of_day.value
            condition.track_condition = command.condition.track_condition.value
            condition.updated_at = stored_changed_at
        self._session.flush()
        return MatchSetupTarget(
            match_id=match.id,
            name=match.name,
            description=match.description,
            source_kind=MatchSourceKind(match.source_kind),
            grade=MatchGrade(match.grade),
            course=course,
            scheduled_at=from_database_utc(match.scheduled_at, field_name="matches.scheduled_at"),
            status=MatchStatus(match.status),
            condition=_condition(condition),
            updated_at=from_database_utc(match.updated_at, field_name="matches.updated_at"),
            setup_version=target.setup_version,
        )

    def add_audit(
        self,
        *,
        command: UpdateMatchSetup,
        before: MatchSetupTarget,
        after: MatchSetupTarget,
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
                match_id=after.match_id,
                type=MatchSetupAuditType.UPDATED.value,
                before_data=before.to_audit_payload(),
                after_data=after.to_audit_payload(),
            )
        )
        self._session.flush()


class SqlAlchemyMatchSetupUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete feature UoW exposing complete Match setup replacement."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._match_setup: SqlAlchemyMatchSetupRepository | None = None

    @property
    def match_setup(self) -> SqlAlchemyMatchSetupRepository:
        return self._require_active_repository(self._match_setup)

    def _activate_repositories(self) -> None:
        self._match_setup = SqlAlchemyMatchSetupRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._match_setup = None


class SqlAlchemyMatchSetupUnitOfWorkFactory(SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchSetupUnitOfWork]):
    """Create one fresh Match setup UoW per final command."""

    unit_of_work_type = SqlAlchemyMatchSetupUnitOfWork
