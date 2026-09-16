"""SQLAlchemy persistence for staff Persona-owned Circle Point operations."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from uma_st2.application.point import (
    MANUAL_ADJUSTMENT_POINT_ACTION,
    MANUAL_GRANT_POINT_ACTION,
    AppliedStaffCirclePoint,
    ApplyStaffCirclePoint,
    StaffCirclePointAuditError,
    StaffCirclePointChoice,
    StaffCirclePointConcurrentConflictError,
    StaffCirclePointIdempotencyConflictError,
    StaffCirclePointOperation,
    StaffCirclePointState,
    StoredStaffCirclePointOperation,
)
from uma_st2.domain.identity import PersonaStatus

from .datetime_codec import from_database_utc, to_database_utc
from .orm import CirclePointORM, OperationORM, PersonaORM, PointTransactionORM
from .registration_bootstrap import is_mariadb_deadlock, sqlite_next_id
from .uow import SessionFactory, SqlAlchemyFeatureUnitOfWork, SqlAlchemyFeatureUnitOfWorkFactory


def _state(
    *,
    guild_id: str,
    persona: PersonaORM,
    wallet: CirclePointORM | None,
) -> StaffCirclePointState:
    return StaffCirclePointState(
        guild_id=guild_id,
        persona_id=persona.id,
        display_name=persona.display_name,
        status=PersonaStatus(persona.status),
        persona_updated_at=from_database_utc(persona.updated_at, field_name="personas.updated_at"),
        balance=None if wallet is None else wallet.balance,
        wallet_updated_at=(
            None if wallet is None else from_database_utc(wallet.updated_at, field_name="circle_points.updated_at")
        ),
    )


class SqlAlchemyStaffCirclePointQueryRepository:
    """Return detached Persona/wallet choices and Preview authority."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def search_targets(self, *, query: str, limit: int) -> tuple[StaffCirclePointChoice, ...]:
        statement = select(PersonaORM, CirclePointORM.persona_id).outerjoin(
            CirclePointORM, CirclePointORM.persona_id == PersonaORM.id
        )
        if query:
            statement = statement.where(
                or_(
                    PersonaORM.id == query,
                    PersonaORM.display_name.contains(query, autoescape=True),
                )
            )
        rows = self._session.execute(
            statement.order_by(PersonaORM.display_name.asc(), PersonaORM.id.asc()).limit(limit)
        ).all()
        return tuple(
            StaffCirclePointChoice(
                persona_id=persona.id,
                display_name=persona.display_name,
                status=PersonaStatus(persona.status),
                wallet_available=wallet_persona_id is not None,
            )
            for persona, wallet_persona_id in rows
        )

    def get_state(self, *, guild_id: str, persona_id: str) -> StaffCirclePointState | None:
        row = self._session.execute(
            select(PersonaORM, CirclePointORM)
            .outerjoin(CirclePointORM, CirclePointORM.persona_id == PersonaORM.id)
            .where(PersonaORM.id == persona_id)
        ).one_or_none()
        if row is None:
            return None
        return _state(guild_id=guild_id, persona=row[0], wallet=row[1])


class SqlAlchemyStaffCirclePointRepository:
    """Serialize on Persona/wallet and append one auditable Point transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_state(self, *, guild_id: str, persona_id: str) -> StaffCirclePointState | None:
        try:
            persona = self._session.scalar(select(PersonaORM).where(PersonaORM.id == persona_id).with_for_update())
            if persona is None:
                return None
            wallet = self._session.scalar(
                select(CirclePointORM).where(CirclePointORM.persona_id == persona_id).with_for_update()
            )
        except OperationalError as error:
            self._raise_concurrency(error)
            raise
        return _state(guild_id=guild_id, persona=persona, wallet=wallet)

    def find_operation(self, *, idempotency_key: str) -> StoredStaffCirclePointOperation | None:
        rows = self._session.execute(
            select(
                OperationORM.request_fingerprint,
                PointTransactionORM.id,
                PointTransactionORM.persona_id,
                PointTransactionORM.action,
                PointTransactionORM.amount,
                PointTransactionORM.created_at,
                PersonaORM.display_name,
                PersonaORM.status,
                CirclePointORM.balance,
                OperationORM.reason,
            )
            .outerjoin(PointTransactionORM, PointTransactionORM.operation_id == OperationORM.id)
            .outerjoin(PersonaORM, PersonaORM.id == PointTransactionORM.persona_id)
            .outerjoin(CirclePointORM, CirclePointORM.persona_id == PointTransactionORM.persona_id)
            .where(OperationORM.idempotency_key == idempotency_key)
            .with_for_update()
        ).all()
        if not rows:
            return None
        request_fingerprint = rows[0].request_fingerprint
        if len(rows) != 1:
            return StoredStaffCirclePointOperation(request_fingerprint=request_fingerprint, result=None)
        row = rows[0]
        operation = {
            MANUAL_GRANT_POINT_ACTION: StaffCirclePointOperation.GRANT,
            MANUAL_ADJUSTMENT_POINT_ACTION: StaffCirclePointOperation.ADJUSTMENT,
        }.get(row.action)
        if (
            operation is None
            or row.id is None
            or row.persona_id is None
            or row.amount is None
            or row.created_at is None
            or row.display_name is None
            or row.status is None
            or row.balance is None
            or row.reason is None
        ):
            return StoredStaffCirclePointOperation(request_fingerprint=request_fingerprint, result=None)
        try:
            result = AppliedStaffCirclePoint(
                transaction_id=row.id,
                persona_id=row.persona_id,
                display_name=row.display_name,
                status=PersonaStatus(row.status),
                operation=operation,
                action=row.action,
                amount=row.amount,
                current_balance=row.balance,
                reason=row.reason,
                created_at=from_database_utc(
                    row.created_at,
                    field_name="point_transactions.created_at",
                ),
            )
        except (TypeError, ValueError):
            return StoredStaffCirclePointOperation(request_fingerprint=request_fingerprint, result=None)
        return StoredStaffCirclePointOperation(request_fingerprint=request_fingerprint, result=result)

    def apply(
        self,
        *,
        command: ApplyStaffCirclePoint,
        state: StaffCirclePointState,
        resulting_balance: int,
        created_at: datetime,
    ) -> AppliedStaffCirclePoint:
        persona = self._session.get(PersonaORM, state.persona_id)
        wallet = self._session.get(CirclePointORM, state.persona_id)
        if persona is None or wallet is None:
            raise StaffCirclePointConcurrentConflictError("The locked Persona wallet disappeared.")
        stored_at = to_database_utc(created_at, field_name="created_at")
        operation_values: dict[str, object] = {
            "guild_id": command.guild_id,
            "correlation_id": command.correlation_id,
            "actor_discord_user_id": command.actor_discord_user_id,
            "idempotency_key": command.idempotency_key,
            "request_fingerprint": command.request_fingerprint,
            "reason": command.reason,
            "created_at": stored_at,
        }
        if self._session.get_bind().dialect.name == "sqlite":
            operation_values["id"] = sqlite_next_id(self._session, OperationORM.id)
        operation = OperationORM(**operation_values)
        self._session.add(operation)
        try:
            self._session.flush()
        except IntegrityError as error:
            raise StaffCirclePointIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            ) from error
        except OperationalError as error:
            self._raise_concurrency(error)
            raise

        wallet.balance = resulting_balance
        wallet.updated_at = stored_at
        transaction_values: dict[str, object] = {
            "persona_id": state.persona_id,
            "operation_id": operation.id,
            "action": command.operation.point_action,
            "amount": command.amount,
            "created_at": stored_at,
        }
        if self._session.get_bind().dialect.name == "sqlite":
            transaction_values["id"] = sqlite_next_id(self._session, PointTransactionORM.id)
        transaction = PointTransactionORM(**transaction_values)
        self._session.add(transaction)
        try:
            self._session.flush()
        except (IntegrityError, OperationalError) as error:
            if isinstance(error, OperationalError):
                self._raise_concurrency(error)
            raise StaffCirclePointAuditError("Staff Circle Point transaction could not be stored.") from error
        return AppliedStaffCirclePoint(
            transaction_id=transaction.id,
            persona_id=state.persona_id,
            display_name=state.display_name,
            status=state.status,
            operation=command.operation,
            action=transaction.action,
            amount=transaction.amount,
            current_balance=wallet.balance,
            reason=command.reason,
            created_at=created_at,
        )

    @staticmethod
    def _raise_concurrency(error: OperationalError) -> None:
        if is_mariadb_deadlock(error):
            raise StaffCirclePointConcurrentConflictError(
                "Concurrent Point mutation rolled back this operation."
            ) from error


class SqlAlchemyStaffCirclePointQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._staff_circle_points: SqlAlchemyStaffCirclePointQueryRepository | None = None

    @property
    def staff_circle_points(self) -> SqlAlchemyStaffCirclePointQueryRepository:
        return self._require_active_repository(self._staff_circle_points)

    def _activate_repositories(self) -> None:
        self._staff_circle_points = SqlAlchemyStaffCirclePointQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._staff_circle_points = None


class SqlAlchemyStaffCirclePointQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyStaffCirclePointQueryUnitOfWork]
):
    unit_of_work_type = SqlAlchemyStaffCirclePointQueryUnitOfWork


class SqlAlchemyStaffCirclePointUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._staff_circle_points: SqlAlchemyStaffCirclePointRepository | None = None

    @property
    def staff_circle_points(self) -> SqlAlchemyStaffCirclePointRepository:
        return self._require_active_repository(self._staff_circle_points)

    def _activate_repositories(self) -> None:
        self._staff_circle_points = SqlAlchemyStaffCirclePointRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._staff_circle_points = None


class SqlAlchemyStaffCirclePointUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyStaffCirclePointUnitOfWork]
):
    unit_of_work_type = SqlAlchemyStaffCirclePointUnitOfWork
