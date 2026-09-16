"""SQLAlchemy projection reader for current Circle Point exports."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from uma_st2.application.exporting import (
    CirclePointExportSource,
    CirclePointExportTransaction,
    CirclePointExportWallet,
)
from uma_st2.domain.identity import PersonaStatus

from .datetime_codec import from_database_utc
from .orm import CirclePointORM, OperationORM, PersonaORM, PointTransactionORM
from .uow import SessionFactory, SqlAlchemyFeatureUnitOfWork, SqlAlchemyFeatureUnitOfWorkFactory


class SqlAlchemyCirclePointExportRepository:
    """Materialize detached current wallets and retained Point transactions."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_current_source(self, *, source_cutoff: datetime) -> CirclePointExportSource:
        wallet_rows = tuple(
            self._session.execute(
                select(CirclePointORM, PersonaORM)
                .join(PersonaORM, PersonaORM.id == CirclePointORM.persona_id)
                .order_by(PersonaORM.id)
            ).all()
        )
        transaction_rows = tuple(
            self._session.execute(
                select(PointTransactionORM, PersonaORM.display_name, OperationORM.reason)
                .join(PersonaORM, PersonaORM.id == PointTransactionORM.persona_id)
                .outerjoin(OperationORM, OperationORM.id == PointTransactionORM.operation_id)
                .order_by(PointTransactionORM.created_at, PointTransactionORM.id)
            ).all()
        )
        return CirclePointExportSource(
            source_cutoff=source_cutoff,
            wallets=tuple(
                CirclePointExportWallet(
                    persona_id=persona.id,
                    display_name=persona.display_name,
                    status=PersonaStatus(persona.status),
                    balance=wallet.balance,
                    updated_at=from_database_utc(wallet.updated_at, field_name="circle_points.updated_at"),
                )
                for wallet, persona in wallet_rows
            ),
            transactions=tuple(
                CirclePointExportTransaction(
                    id=transaction.id,
                    persona_id=transaction.persona_id,
                    display_name=display_name,
                    action=transaction.action,
                    amount=transaction.amount,
                    created_at=from_database_utc(
                        transaction.created_at,
                        field_name="point_transactions.created_at",
                    ),
                    operation_id=transaction.operation_id,
                    reason=reason,
                )
                for transaction, display_name, reason in transaction_rows
            ),
        )


class SqlAlchemyCirclePointExportUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW exposing the Circle Point export projection."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._exports: SqlAlchemyCirclePointExportRepository | None = None

    @property
    def circle_point_exports(self) -> SqlAlchemyCirclePointExportRepository:
        return self._require_active_repository(self._exports)

    def _activate_repositories(self) -> None:
        self._exports = SqlAlchemyCirclePointExportRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._exports = None


class SqlAlchemyCirclePointExportUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyCirclePointExportUnitOfWork]
):
    """Create one fresh Circle Point export query UoW."""

    unit_of_work_type = SqlAlchemyCirclePointExportUnitOfWork
