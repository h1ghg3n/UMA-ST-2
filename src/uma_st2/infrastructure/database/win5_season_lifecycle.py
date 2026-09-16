"""SQLAlchemy implementation of WIN5 Season management commands."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from uma_st2.application.win5.season_lifecycle import (
    CreateWin5Season,
    StoredWin5SeasonOperation,
    TransitionWin5Season,
    UpdateWin5SeasonMetadata,
    Win5SeasonActivationConflictError,
    Win5SeasonAuditRecord,
    Win5SeasonRoundState,
    Win5SeasonSnapshot,
)
from uma_st2.domain.win5 import Win5RoundStatus, Win5SeasonStatus

from .datetime_codec import from_database_utc, to_database_utc
from .orm import OperationORM, Win5OperationORM, Win5RoundORM, Win5SeasonORM
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)
from .win5_season_marker import (
    validate_win5_season_marker,
    win5_season_active_marker,
)


class SqlAlchemyWin5SeasonLifecycleRepository:
    """Lock, mutate, and audit one Season through an active Session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find_operation(self, *, idempotency_key: str) -> StoredWin5SeasonOperation | None:
        row = self._session.execute(
            select(
                OperationORM.request_fingerprint,
                Win5OperationORM.type,
                Win5OperationORM.season_id,
                Win5OperationORM.after_data,
            )
            .outerjoin(Win5OperationORM, Win5OperationORM.operation_id == OperationORM.id)
            .where(OperationORM.idempotency_key == idempotency_key)
            .with_for_update()
        ).one_or_none()
        if row is None:
            return None
        return StoredWin5SeasonOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            season_id=row.season_id,
            after_data=row.after_data,
        )

    def lock_season(self, *, season_id: int) -> Win5SeasonSnapshot | None:
        season = self._session.scalar(select(Win5SeasonORM).where(Win5SeasonORM.id == season_id).with_for_update())
        return None if season is None else self._snapshot(season)

    def lock_round_statuses(self, *, season_id: int) -> tuple[Win5RoundStatus, ...]:
        values = self._session.scalars(
            select(Win5RoundORM.status)
            .where(Win5RoundORM.season_id == season_id)
            .order_by(Win5RoundORM.id)
            .with_for_update()
        ).all()
        return tuple(Win5RoundStatus(value) for value in values)

    def create_draft(
        self,
        *,
        command: CreateWin5Season,
        created_at: datetime,
    ) -> Win5SeasonSnapshot:
        stored_created_at = to_database_utc(created_at, field_name="created_at")
        season = Win5SeasonORM(
            name=command.name,
            status=Win5SeasonStatus.DRAFT.value,
            active_marker=None,
            starts_at=(
                None if command.starts_at is None else to_database_utc(command.starts_at, field_name="starts_at")
            ),
            ends_at=(None if command.ends_at is None else to_database_utc(command.ends_at, field_name="ends_at")),
            created_at=stored_created_at,
            updated_at=stored_created_at,
        )
        self._session.add(season)
        self._session.flush()
        return self._snapshot(season)

    def update_status(
        self,
        *,
        season_id: int,
        expected_status: Win5SeasonStatus,
        target_status: Win5SeasonStatus,
        changed_at: datetime,
    ) -> None:
        try:
            changed = self._session.execute(
                update(Win5SeasonORM)
                .where(
                    Win5SeasonORM.id == season_id,
                    Win5SeasonORM.status == expected_status.value,
                    Win5SeasonORM.active_marker.is_(win5_season_active_marker(expected_status)),
                )
                .values(
                    status=target_status.value,
                    active_marker=win5_season_active_marker(target_status),
                    updated_at=to_database_utc(changed_at, field_name="changed_at"),
                )
            )
            if changed.rowcount != 1:
                raise RuntimeError("WIN5 Season changed while its lifecycle transition was being applied.")
            self._session.flush()
        except IntegrityError as exc:
            if target_status == Win5SeasonStatus.ACTIVE:
                raise Win5SeasonActivationConflictError("Another active WIN5 Season already exists.") from exc
            raise

    def update_metadata(
        self,
        *,
        command: UpdateWin5SeasonMetadata,
        changed_at: datetime,
    ) -> None:
        changed = self._session.execute(
            update(Win5SeasonORM)
            .where(Win5SeasonORM.id == command.season_id)
            .values(
                name=command.name,
                starts_at=(
                    None if command.starts_at is None else to_database_utc(command.starts_at, field_name="starts_at")
                ),
                ends_at=(None if command.ends_at is None else to_database_utc(command.ends_at, field_name="ends_at")),
                updated_at=to_database_utc(changed_at, field_name="changed_at"),
            )
        )
        if changed.rowcount != 1:
            raise RuntimeError("WIN5 Season changed while its metadata update was being applied.")
        self._session.flush()

    def add_audit(
        self,
        *,
        command: CreateWin5Season | TransitionWin5Season | UpdateWin5SeasonMetadata,
        record: Win5SeasonAuditRecord,
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
            Win5OperationORM(
                operation_id=operation.id,
                season_id=record.after.id,
                round_id=None,
                submission_id=None,
                type=record.type.value,
                before_data=record.before_data,
                after_data=record.after_data,
            )
        )
        self._session.flush()

    @staticmethod
    def _snapshot(season: Win5SeasonORM) -> Win5SeasonSnapshot:
        return Win5SeasonSnapshot(
            id=season.id,
            name=season.name,
            status=validate_win5_season_marker(
                status=season.status,
                active_marker=season.active_marker,
            ),
            starts_at=(
                None if season.starts_at is None else from_database_utc(season.starts_at, field_name="starts_at")
            ),
            ends_at=(None if season.ends_at is None else from_database_utc(season.ends_at, field_name="ends_at")),
            rounds=Win5SeasonRoundState(),
        )


class SqlAlchemyWin5SeasonLifecycleUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete feature UoW exposing only Season lifecycle persistence."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._win5_season_lifecycle: SqlAlchemyWin5SeasonLifecycleRepository | None = None

    @property
    def win5_season_lifecycle(self) -> SqlAlchemyWin5SeasonLifecycleRepository:
        return self._require_active_repository(self._win5_season_lifecycle)

    def _activate_repositories(self) -> None:
        self._win5_season_lifecycle = SqlAlchemyWin5SeasonLifecycleRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._win5_season_lifecycle = None


class SqlAlchemyWin5SeasonLifecycleUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyWin5SeasonLifecycleUnitOfWork]
):
    """Create one fresh Season lifecycle UoW per application command."""

    unit_of_work_type = SqlAlchemyWin5SeasonLifecycleUnitOfWork
