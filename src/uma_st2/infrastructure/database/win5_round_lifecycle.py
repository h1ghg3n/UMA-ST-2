"""SQLAlchemy implementation of WIN5 Round open/close commands."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from uma_st2.application.win5.round_lifecycle import (
    StoredWin5RoundLifecycleOperation,
    TransitionWin5Round,
    Win5RoundGraphState,
    Win5RoundLifecycleAuditRecord,
    Win5RoundLifecycleRoundTarget,
    Win5RoundLifecycleSeason,
)
from uma_st2.domain.win5 import Win5RoundSourceKind, Win5RoundStatus, Win5RoundType

from .datetime_codec import to_database_utc
from .orm import (
    OperationORM,
    Win5OperationORM,
    Win5RaceEntryORM,
    Win5RaceORM,
    Win5ResultORM,
    Win5RoundORM,
    Win5SeasonORM,
)
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)
from .win5_season_marker import validate_win5_season_marker


class SqlAlchemyWin5RoundLifecycleRepository:
    """Apply one Round transition through an active Session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find_round_season_id(self, *, round_id: int) -> int | None:
        return self._session.scalar(select(Win5RoundORM.season_id).where(Win5RoundORM.id == round_id))

    def lock_season(self, *, season_id: int) -> Win5RoundLifecycleSeason | None:
        season = self._session.scalar(select(Win5SeasonORM).where(Win5SeasonORM.id == season_id).with_for_update())
        return None if season is None else self._season(season)

    def get_season(self, *, season_id: int) -> Win5RoundLifecycleSeason | None:
        season = self._session.get(Win5SeasonORM, season_id)
        return None if season is None else self._season(season)

    def lock_round(self, *, round_id: int) -> Win5RoundLifecycleRoundTarget | None:
        round_ = self._session.scalar(select(Win5RoundORM).where(Win5RoundORM.id == round_id).with_for_update())
        return None if round_ is None else self._round(round_)

    def find_operation(self, *, idempotency_key: str) -> StoredWin5RoundLifecycleOperation | None:
        row = self._session.execute(
            select(
                OperationORM.request_fingerprint,
                Win5OperationORM.type,
                Win5OperationORM.season_id,
                Win5OperationORM.round_id,
                Win5OperationORM.after_data,
            )
            .outerjoin(Win5OperationORM, Win5OperationORM.operation_id == OperationORM.id)
            .where(OperationORM.idempotency_key == idempotency_key)
            .with_for_update()
        ).one_or_none()
        if row is None:
            return None
        return StoredWin5RoundLifecycleOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            season_id=row.season_id,
            round_id=row.round_id,
            after_data=row.after_data,
        )

    def load_round_graph_state(self, *, round_id: int) -> Win5RoundGraphState:
        race_ids = select(Win5RaceORM.id).where(Win5RaceORM.round_id == round_id)
        # These locking current reads run after the Round root lock. They both
        # freeze the opening graph and avoid reusing the earlier parent-ID
        # lookup snapshot under MariaDB REPEATABLE READ.
        race_count = self._session.scalar(
            select(func.count(Win5RaceORM.id)).where(Win5RaceORM.round_id == round_id).with_for_update()
        )
        race_entry_count = self._session.scalar(
            select(func.count(Win5RaceEntryORM.id)).where(Win5RaceEntryORM.race_id.in_(race_ids)).with_for_update()
        )
        result_count = self._session.scalar(
            select(func.count(Win5ResultORM.id)).where(Win5ResultORM.race_id.in_(race_ids)).with_for_update()
        )
        return Win5RoundGraphState(
            race_count=race_count or 0,
            race_entry_count=race_entry_count or 0,
            result_count=result_count or 0,
        )

    def count_open_rounds(self, *, season_id: int) -> int:
        # The Season root is already locked. A locking current read is still
        # required so MariaDB REPEATABLE READ does not reuse the parent-ID
        # lookup snapshot after a concurrent opener commits.
        return (
            self._session.scalar(
                select(func.count(Win5RoundORM.id))
                .where(
                    Win5RoundORM.season_id == season_id,
                    Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
                    Win5RoundORM.status == Win5RoundStatus.OPEN.value,
                )
                .with_for_update()
            )
            or 0
        )

    def update_round_status(
        self,
        *,
        round_id: int,
        expected_status: Win5RoundStatus,
        target_status: Win5RoundStatus,
        changed_at: datetime,
    ) -> None:
        changed = self._session.execute(
            update(Win5RoundORM)
            .where(
                Win5RoundORM.id == round_id,
                Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
                Win5RoundORM.status == expected_status.value,
            )
            .values(
                status=target_status.value,
                updated_at=to_database_utc(changed_at, field_name="changed_at"),
            )
        )
        if changed.rowcount != 1:
            raise RuntimeError("WIN5 Round changed while its lifecycle transition was being applied.")
        self._session.flush()

    def add_lifecycle_audit(
        self,
        *,
        command: TransitionWin5Round,
        record: Win5RoundLifecycleAuditRecord,
        created_at: datetime,
    ) -> None:
        stored_created_at = to_database_utc(created_at, field_name="created_at")
        operation = OperationORM(
            guild_id=command.guild_id,
            correlation_id=command.correlation_id,
            actor_discord_user_id=command.actor_discord_user_id,
            idempotency_key=command.idempotency_key,
            request_fingerprint=command.request_fingerprint,
            reason=command.reason,
            created_at=stored_created_at,
        )
        self._session.add(operation)
        self._session.flush()
        self._session.add(
            Win5OperationORM(
                operation_id=operation.id,
                season_id=record.after.season_id,
                round_id=record.after.round_id,
                submission_id=None,
                type=record.type.value,
                before_data=record.before_data,
                after_data=record.after_data,
            )
        )
        self._session.flush()

    @staticmethod
    def _season(season: Win5SeasonORM) -> Win5RoundLifecycleSeason:
        return Win5RoundLifecycleSeason(
            id=season.id,
            name=season.name,
            status=validate_win5_season_marker(
                status=season.status,
                active_marker=season.active_marker,
            ),
        )

    @staticmethod
    def _round(round_: Win5RoundORM) -> Win5RoundLifecycleRoundTarget:
        return Win5RoundLifecycleRoundTarget(
            id=round_.id,
            season_id=round_.season_id,
            name=round_.name,
            type=Win5RoundType(round_.type),
            status=Win5RoundStatus(round_.status),
            source_kind=Win5RoundSourceKind(round_.source_kind),
        )


class SqlAlchemyWin5RoundLifecycleUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete feature UoW exposing only its Round lifecycle repository."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._win5_round_lifecycle: SqlAlchemyWin5RoundLifecycleRepository | None = None

    @property
    def win5_round_lifecycle(self) -> SqlAlchemyWin5RoundLifecycleRepository:
        return self._require_active_repository(self._win5_round_lifecycle)

    def _activate_repositories(self) -> None:
        self._win5_round_lifecycle = SqlAlchemyWin5RoundLifecycleRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._win5_round_lifecycle = None


class SqlAlchemyWin5RoundLifecycleUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyWin5RoundLifecycleUnitOfWork]
):
    """Create one Round lifecycle UoW per application command."""

    unit_of_work_type = SqlAlchemyWin5RoundLifecycleUnitOfWork
