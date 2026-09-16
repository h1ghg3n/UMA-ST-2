"""SQLAlchemy implementation of native V2 Circle Match condition mutation."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from uma_st2.application.match import (
    MATCH_CONDITION_AUDIT_SCHEMA_VERSION,
    MatchConditionAuditType,
    MatchConditionRecord,
    MatchConditionTarget,
    MatchConditionValues,
    SetMatchConditions,
    StoredMatchConditionOperation,
)
from uma_st2.domain.match import MatchSourceKind, MatchStatus

from .datetime_codec import from_database_utc, to_database_utc
from .orm import MatchConditionORM, MatchOperationORM, MatchORM, OperationORM
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)

_CONDITION_OPERATION_TYPES = tuple(value.value for value in MatchConditionAuditType)


class SqlAlchemyMatchConditionRepository:
    """Lock, replace, and audit complete Match conditions in one Session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_target(self, *, match_id: int) -> MatchConditionTarget | None:
        match = self._session.scalar(select(MatchORM).where(MatchORM.id == match_id).with_for_update())
        if match is None:
            return None
        condition = self._session.scalar(
            select(MatchConditionORM).where(MatchConditionORM.match_id == match_id).with_for_update()
        )
        condition_version = self._session.scalar(
            select(func.max(MatchOperationORM.operation_id)).where(
                MatchOperationORM.match_id == match_id,
                MatchOperationORM.type.in_(_CONDITION_OPERATION_TYPES),
            )
        )
        return self._target(match=match, condition=condition, condition_version=condition_version)

    def find_operation(self, *, idempotency_key: str) -> StoredMatchConditionOperation | None:
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
        return StoredMatchConditionOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            match_id=row.match_id,
            after_data=row.after_data,
        )

    def replace_conditions(
        self,
        *,
        target: MatchConditionTarget,
        values: MatchConditionValues,
        changed_at: datetime,
    ) -> MatchConditionTarget:
        condition = self._session.get(MatchConditionORM, target.match_id)
        stored_changed_at = to_database_utc(changed_at, field_name="changed_at")
        if condition is None:
            condition = MatchConditionORM(
                match_id=target.match_id,
                season=values.season.value,
                weather=values.weather.value,
                time_of_day=values.time_of_day.value,
                track_condition=values.track_condition.value,
                created_at=stored_changed_at,
                updated_at=stored_changed_at,
            )
            self._session.add(condition)
        else:
            condition.season = values.season.value
            condition.weather = values.weather.value
            condition.time_of_day = values.time_of_day.value
            condition.track_condition = values.track_condition.value
            condition.updated_at = stored_changed_at
        self._session.flush()
        return MatchConditionTarget(
            match_id=target.match_id,
            name=target.name,
            source_kind=target.source_kind,
            status=target.status,
            scheduled_at=target.scheduled_at,
            condition=self._condition(condition),
            condition_version=target.condition_version,
        )

    def add_audit(
        self,
        *,
        command: SetMatchConditions,
        operation_type: MatchConditionAuditType,
        before: MatchConditionRecord | None,
        after: MatchConditionTarget,
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
        before_data = None
        if before is not None:
            before_data = {
                "schema_version": MATCH_CONDITION_AUDIT_SCHEMA_VERSION,
                "condition": before.to_audit_payload(),
            }
        self._session.add(
            MatchOperationORM(
                operation_id=operation.id,
                match_id=after.match_id,
                type=operation_type.value,
                before_data=before_data,
                after_data=after.to_audit_payload(),
            )
        )
        self._session.flush()

    @staticmethod
    def _target(
        *,
        match: MatchORM,
        condition: MatchConditionORM | None,
        condition_version: int | None,
    ) -> MatchConditionTarget:
        return MatchConditionTarget(
            match_id=match.id,
            name=match.name,
            source_kind=MatchSourceKind(match.source_kind),
            status=MatchStatus(match.status),
            scheduled_at=from_database_utc(match.scheduled_at, field_name="scheduled_at"),
            condition=None if condition is None else SqlAlchemyMatchConditionRepository._condition(condition),
            condition_version=condition_version,
        )

    @staticmethod
    def _condition(condition: MatchConditionORM) -> MatchConditionRecord:
        return MatchConditionRecord(
            values=MatchConditionValues(
                season=condition.season,
                weather=condition.weather,
                time_of_day=condition.time_of_day,
                track_condition=condition.track_condition,
            ),
            created_at=from_database_utc(condition.created_at, field_name="condition.created_at"),
            updated_at=from_database_utc(condition.updated_at, field_name="condition.updated_at"),
        )


class SqlAlchemyMatchConditionUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete feature UoW exposing native Match condition mutation."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._match_conditions: SqlAlchemyMatchConditionRepository | None = None

    @property
    def match_conditions(self) -> SqlAlchemyMatchConditionRepository:
        return self._require_active_repository(self._match_conditions)

    def _activate_repositories(self) -> None:
        self._match_conditions = SqlAlchemyMatchConditionRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._match_conditions = None


class SqlAlchemyMatchConditionUnitOfWorkFactory(SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchConditionUnitOfWork]):
    """Create one Match condition UoW per application command."""

    unit_of_work_type = SqlAlchemyMatchConditionUnitOfWork
