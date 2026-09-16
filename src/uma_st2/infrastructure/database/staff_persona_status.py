"""SQLAlchemy persistence for staff Persona status management."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from uma_st2.application.identity.status_management import (
    ChangedPersonaStatus,
    ChangePersonaStatus,
    StaffPersonaStatusAuditError,
    StaffPersonaStatusAuditType,
    StaffPersonaStatusConcurrentConflictError,
    StaffPersonaStatusIdempotencyConflictError,
    StaffPersonaStatusState,
    StoredStaffPersonaStatusOperation,
)
from uma_st2.domain.identity import PersonaStatus

from .datetime_codec import to_database_utc
from .orm import IdentityOperationORM, OperationORM, PersonaORM
from .uow import SessionFactory, SqlAlchemyFeatureUnitOfWork, SqlAlchemyFeatureUnitOfWorkFactory


def _sqlite_next_id(session: Session, column: object) -> int:
    value = session.scalar(select(func.coalesce(func.max(column), 0) + 1))
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError("SQLite test identity allocation failed.")
    return value


def _is_deadlock(error: OperationalError) -> bool:
    arguments = getattr(error.orig, "args", ())
    return bool(arguments) and arguments[0] == 1213


def _state(*, guild_id: str, persona: PersonaORM) -> StaffPersonaStatusState:
    return StaffPersonaStatusState(
        guild_id=guild_id,
        persona_id=persona.id,
        display_name=persona.display_name,
        status=PersonaStatus(persona.status),
    )


class SqlAlchemyStaffPersonaStatusQueryRepository:
    """Return detached Persona status without locks or writes."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_state(self, *, guild_id: str, persona_id: str) -> StaffPersonaStatusState | None:
        persona = self._session.get(PersonaORM, persona_id)
        return None if persona is None else _state(guild_id=guild_id, persona=persona)


class SqlAlchemyStaffPersonaStatusRepository:
    """Serialize on Persona and persist one audited status change."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_state(self, *, guild_id: str, persona_id: str) -> StaffPersonaStatusState | None:
        try:
            persona = self._session.scalar(select(PersonaORM).where(PersonaORM.id == persona_id).with_for_update())
        except OperationalError as error:
            self._raise_concurrency(error)
            raise
        return None if persona is None else _state(guild_id=guild_id, persona=persona)

    def find_operation(self, *, idempotency_key: str) -> StoredStaffPersonaStatusOperation | None:
        row = self._session.execute(
            select(
                OperationORM.request_fingerprint,
                IdentityOperationORM.type,
                IdentityOperationORM.persona_id,
                IdentityOperationORM.game_account_id,
                IdentityOperationORM.after_data,
            )
            .outerjoin(IdentityOperationORM, IdentityOperationORM.operation_id == OperationORM.id)
            .where(OperationORM.idempotency_key == idempotency_key)
            .with_for_update()
        ).one_or_none()
        if row is None:
            return None
        return StoredStaffPersonaStatusOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            persona_id=row.persona_id,
            game_account_id=row.game_account_id,
            after_data=row.after_data,
        )

    def change_status(
        self,
        *,
        command: ChangePersonaStatus,
        state: StaffPersonaStatusState,
        updated_at: datetime,
    ) -> ChangedPersonaStatus:
        persona = self._session.get(PersonaORM, state.persona_id)
        if persona is None:
            raise StaffPersonaStatusConcurrentConflictError("The locked Persona disappeared.")
        stored_at = to_database_utc(updated_at, field_name="updated_at")
        result = ChangedPersonaStatus(
            persona_id=state.persona_id,
            display_name=state.display_name,
            previous_status=state.status,
            status=command.desired_status,
            reason=command.reason,
            updated_at=updated_at,
        )
        persona.status = command.desired_status.value
        persona.updated_at = stored_at
        operation = self._add_operation(
            guild_id=command.guild_id,
            correlation_id=command.correlation_id,
            actor_discord_user_id=command.updated_by_discord_user_id,
            idempotency_key=command.idempotency_key,
            request_fingerprint=command.request_fingerprint,
            reason=command.reason,
            created_at=stored_at,
        )
        self._session.add(
            IdentityOperationORM(
                operation_id=operation.id,
                persona_id=state.persona_id,
                game_account_id=None,
                discord_user_id=None,
                type=StaffPersonaStatusAuditType.PERSONA_STATUS_CHANGED.value,
                before_data=state.to_audit_payload(),
                after_data=result.to_audit_payload(),
            )
        )
        self._flush_audit()
        return result

    def _add_operation(
        self,
        *,
        guild_id: str,
        correlation_id: str | None,
        actor_discord_user_id: str,
        idempotency_key: str,
        request_fingerprint: str,
        reason: str,
        created_at: datetime,
    ) -> OperationORM:
        values: dict[str, object] = {
            "guild_id": guild_id,
            "correlation_id": correlation_id,
            "actor_discord_user_id": actor_discord_user_id,
            "idempotency_key": idempotency_key,
            "request_fingerprint": request_fingerprint,
            "reason": reason,
            "created_at": created_at,
        }
        if self._session.get_bind().dialect.name == "sqlite":
            values["id"] = _sqlite_next_id(self._session, OperationORM.id)
        operation = OperationORM(**values)
        self._session.add(operation)
        try:
            self._session.flush()
        except IntegrityError as error:
            raise StaffPersonaStatusIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            ) from error
        except OperationalError as error:
            self._raise_concurrency(error)
            raise
        return operation

    def _flush_audit(self) -> None:
        try:
            self._session.flush()
        except (IntegrityError, OperationalError) as error:
            if isinstance(error, OperationalError):
                self._raise_concurrency(error)
            raise StaffPersonaStatusAuditError("Persona status audit could not be stored.") from error

    @staticmethod
    def _raise_concurrency(error: OperationalError) -> None:
        if _is_deadlock(error):
            raise StaffPersonaStatusConcurrentConflictError(
                "Concurrent Identity mutation rolled back this status change."
            ) from error


class SqlAlchemyStaffPersonaStatusQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._staff_persona_status: SqlAlchemyStaffPersonaStatusQueryRepository | None = None

    @property
    def staff_persona_status(self) -> SqlAlchemyStaffPersonaStatusQueryRepository:
        return self._require_active_repository(self._staff_persona_status)

    def _activate_repositories(self) -> None:
        self._staff_persona_status = SqlAlchemyStaffPersonaStatusQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._staff_persona_status = None


class SqlAlchemyStaffPersonaStatusQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyStaffPersonaStatusQueryUnitOfWork]
):
    unit_of_work_type = SqlAlchemyStaffPersonaStatusQueryUnitOfWork


class SqlAlchemyStaffPersonaStatusUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._staff_persona_status: SqlAlchemyStaffPersonaStatusRepository | None = None

    @property
    def staff_persona_status(self) -> SqlAlchemyStaffPersonaStatusRepository:
        return self._require_active_repository(self._staff_persona_status)

    def _activate_repositories(self) -> None:
        self._staff_persona_status = SqlAlchemyStaffPersonaStatusRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._staff_persona_status = None


class SqlAlchemyStaffPersonaStatusUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyStaffPersonaStatusUnitOfWork]
):
    unit_of_work_type = SqlAlchemyStaffPersonaStatusUnitOfWork
