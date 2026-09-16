"""SQLAlchemy implementation of native Match result publication and recovery."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from uma_st2.application.match import (
    MatchResultPublicationAuditType,
    MatchResultPublicationLock,
    MatchResultPublicationTarget,
    MatchResultPublicationTargetChoice,
    PublishedMatchResult,
    PublishMatchResult,
    StoredMatchResultPublication,
    StoredMatchResultPublicationOperation,
)
from uma_st2.application.match.settlement import MatchSettlementAuditType, SettledMatch
from uma_st2.application.publication import (
    MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    MATCH_PUBLICATION_SOURCE_KIND,
    MATCH_RESULT_CONFIRMED_EVENT_TYPE,
    PublicationIntent,
)
from uma_st2.application.publication.match import (
    MatchOpeningCondition,
    MatchPublicationDestination,
    MatchResultCourse,
)
from uma_st2.domain.match import (
    MatchDirection,
    MatchGrade,
    MatchSeason,
    MatchSourceKind,
    MatchStatus,
    MatchSurface,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
    StadiumCourseLayout,
)
from uma_st2.domain.publication import PublicationStatus

from .datetime_codec import from_database_utc, to_database_utc
from .orm import (
    BotGuildSettingORM,
    DiscordPublicationORM,
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


class SqlAlchemyMatchResultPublicationRepository:
    """Consume settlement evidence and append or recover one public intent."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_match(self, *, match_id: int) -> MatchResultPublicationLock | None:
        match = self._session.scalar(select(MatchORM).where(MatchORM.id == match_id).with_for_update())
        if match is None:
            return None
        return MatchResultPublicationLock(
            match_id=match.id,
            match_name=match.name,
            source_kind=MatchSourceKind(match.source_kind),
            status=MatchStatus(match.status),
        )

    def find_operation(self, *, idempotency_key: str) -> StoredMatchResultPublicationOperation | None:
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
        return StoredMatchResultPublicationOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            match_id=row.match_id,
            after_data=row.after_data,
        )

    def find_result_publication(self, *, match_id: int) -> StoredMatchResultPublication | None:
        rows = tuple(
            self._session.scalars(
                select(DiscordPublicationORM)
                .where(
                    DiscordPublicationORM.destination_kind == MATCH_ANNOUNCEMENT_DESTINATION_KIND,
                    DiscordPublicationORM.event_type == MATCH_RESULT_CONFIRMED_EVENT_TYPE,
                    DiscordPublicationORM.source_kind == MATCH_PUBLICATION_SOURCE_KIND,
                    DiscordPublicationORM.source_id == match_id,
                )
                .order_by(DiscordPublicationORM.id)
                .limit(2)
                .with_for_update()
            )
        )
        if len(rows) > 1:
            raise ValueError("Match has more than one logical settled-result publication.")
        if not rows:
            return None
        row = rows[0]
        return StoredMatchResultPublication(
            publication_id=row.id,
            event_key=row.event_key,
            payload_fingerprint=row.payload_fingerprint,
            status=PublicationStatus(row.status),
            target_channel_id=row.target_channel_id,
        )

    def load_target(self, *, match_id: int, guild_id: str) -> MatchResultPublicationTarget | None:
        identity = self._session.execute(
            select(MatchORM, StadiumCourseORM, StadiumORM)
            .join(StadiumCourseORM, StadiumCourseORM.id == MatchORM.stadium_course_id)
            .join(StadiumORM, StadiumORM.id == StadiumCourseORM.stadium_id)
            .where(MatchORM.id == match_id)
        ).one_or_none()
        if identity is None:
            return None
        match, course, stadium = identity
        condition = self._session.get(MatchConditionORM, match_id)
        if condition is None:
            raise ValueError("Settled Match has no complete condition row.")

        settlement_rows = tuple(
            self._session.scalars(
                select(MatchOperationORM.after_data)
                .where(
                    MatchOperationORM.match_id == match_id,
                    MatchOperationORM.type == MatchSettlementAuditType.SETTLED.value,
                )
                .order_by(MatchOperationORM.operation_id)
                .limit(2)
            )
        )
        if len(settlement_rows) != 1 or settlement_rows[0] is None:
            raise ValueError("Settled Match must retain exactly one complete settlement operation.")
        settlement = SettledMatch.from_audit_payload(settlement_rows[0])

        setting = self._session.get(BotGuildSettingORM, guild_id)
        destination = MatchPublicationDestination(
            guild_id=guild_id,
            announcements_enabled=True if setting is None else setting.match_announcements_enabled,
            target_channel_id=None if setting is None else setting.match_announcement_channel_id,
        )
        return MatchResultPublicationTarget(
            match_id=match.id,
            match_name=match.name,
            source_kind=MatchSourceKind(match.source_kind),
            status=MatchStatus(match.status),
            grade=MatchGrade(match.grade),
            scheduled_at=from_database_utc(match.scheduled_at, field_name="matches.scheduled_at"),
            course=MatchResultCourse(
                stadium_name=stadium.name_ko or stadium.name_jp,
                surface=MatchSurface(course.surface),
                distance=course.distance,
                direction=MatchDirection(course.direction),
                layout=StadiumCourseLayout(course.layout),
            ),
            condition=MatchOpeningCondition(
                season=MatchSeason(condition.season),
                weather=MatchWeather(condition.weather),
                time_of_day=MatchTimeOfDay(condition.time_of_day),
                track_condition=MatchTrackCondition(condition.track_condition),
            ),
            destination=destination,
            settlement=settlement,
        )

    def add_publication(
        self,
        *,
        intent: PublicationIntent,
        created_at: datetime,
    ) -> StoredMatchResultPublication:
        stored_at = to_database_utc(created_at, field_name="created_at")
        publication = DiscordPublicationORM(
            guild_id=intent.guild_id,
            destination_kind=intent.destination_kind,
            event_type=intent.event_type,
            event_key=intent.event_key,
            source_kind=intent.source_kind,
            source_id=intent.source_id,
            target_channel_id=intent.target_channel_id,
            payload_json=intent.payload_json,
            payload_fingerprint=intent.payload_fingerprint,
            status=intent.status.value,
            attempt_count=0,
            discord_message_id=None,
            last_error_code=None,
            failure_stage=None,
            attempt_started_at=None,
            published_at=None,
            created_at=stored_at,
            updated_at=stored_at,
        )
        self._session.add(publication)
        self._session.flush()
        return StoredMatchResultPublication(
            publication_id=publication.id,
            event_key=publication.event_key,
            payload_fingerprint=publication.payload_fingerprint,
            status=PublicationStatus(publication.status),
            target_channel_id=publication.target_channel_id,
        )

    def add_audit(
        self,
        *,
        command: PublishMatchResult,
        before: MatchResultPublicationTarget,
        after: PublishedMatchResult,
        created_at: datetime,
    ) -> None:
        operation = OperationORM(
            guild_id=command.guild_id,
            correlation_id=command.correlation_id,
            actor_discord_user_id=command.actor_discord_user_id,
            idempotency_key=command.idempotency_key,
            request_fingerprint=command.request_fingerprint,
            reason=None,
            created_at=to_database_utc(created_at, field_name="created_at"),
        )
        self._session.add(operation)
        self._session.flush()
        self._session.add(
            MatchOperationORM(
                operation_id=operation.id,
                match_id=after.match_id,
                type=MatchResultPublicationAuditType.PUBLISHED.value,
                before_data=before.to_audit_payload(),
                after_data=after.to_audit_payload(),
            )
        )
        self._session.flush()


class SqlAlchemyMatchResultPublicationQueryRepository:
    """List native settled Matches without a result publication intent."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchResultPublicationTargetChoice, ...]:
        already_published = exists(
            select(DiscordPublicationORM.id).where(
                DiscordPublicationORM.destination_kind == MATCH_ANNOUNCEMENT_DESTINATION_KIND,
                DiscordPublicationORM.event_type == MATCH_RESULT_CONFIRMED_EVENT_TYPE,
                DiscordPublicationORM.source_kind == MATCH_PUBLICATION_SOURCE_KIND,
                DiscordPublicationORM.source_id == MatchORM.id,
            )
        )
        statement = (
            select(MatchORM.id, MatchORM.name, MatchORM.grade)
            .where(
                MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
                MatchORM.status == MatchStatus.SETTLED.value,
                ~already_published,
            )
            .order_by(MatchORM.scheduled_at.desc(), MatchORM.id.desc())
            .limit(limit)
        )
        if search:
            pattern = f"%{search}%"
            numeric_id = int(search) if search.isascii() and search.isdecimal() else None
            statement = statement.where(
                or_(
                    MatchORM.name.like(pattern),
                    MatchORM.id == numeric_id if numeric_id is not None else False,
                )
            )
        return tuple(
            MatchResultPublicationTargetChoice(
                match_id=row.id,
                match_name=row.name,
                grade=MatchGrade(row.grade),
            )
            for row in self._session.execute(statement)
        )


class SqlAlchemyMatchResultPublicationUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete UoW exposing the result publication mutation repository."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._repository: SqlAlchemyMatchResultPublicationRepository | None = None

    @property
    def match_result_publication(self) -> SqlAlchemyMatchResultPublicationRepository:
        return self._require_active_repository(self._repository)

    def _activate_repositories(self) -> None:
        self._repository = SqlAlchemyMatchResultPublicationRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._repository = None


class SqlAlchemyMatchResultPublicationUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchResultPublicationUnitOfWork]
):
    """Create one fresh command UoW per explicit publish request."""

    unit_of_work_type = SqlAlchemyMatchResultPublicationUnitOfWork


class SqlAlchemyMatchResultPublicationQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW for publish autocomplete targets."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._repository: SqlAlchemyMatchResultPublicationQueryRepository | None = None

    @property
    def match_result_publication_queries(self) -> SqlAlchemyMatchResultPublicationQueryRepository:
        return self._require_active_repository(self._repository)

    def _activate_repositories(self) -> None:
        self._repository = SqlAlchemyMatchResultPublicationQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._repository = None


class SqlAlchemyMatchResultPublicationQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchResultPublicationQueryUnitOfWork]
):
    """Create one fresh query UoW per autocomplete request."""

    unit_of_work_type = SqlAlchemyMatchResultPublicationQueryUnitOfWork
