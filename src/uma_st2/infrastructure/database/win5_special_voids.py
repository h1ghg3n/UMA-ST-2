"""SQLAlchemy persistence for Special WIN5 Race-void mutations."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from uma_st2.application.win5.special_voids import (
    CancelSpecialWin5Round,
    SetSpecialWin5RaceVoid,
    StoredWin5SpecialVoidOperation,
    Win5SpecialRaceVoidFact,
    Win5SpecialRoundCancellationAuditRecord,
    Win5SpecialVoidAuditRecord,
    Win5SpecialVoidResultSnapshot,
    Win5SpecialVoidRoundTarget,
    Win5SpecialVoidStateSnapshot,
)
from uma_st2.domain.win5 import Win5RoundSourceKind, Win5RoundStatus, Win5RoundType

from .datetime_codec import from_database_utc, to_database_utc
from .orm import (
    OperationORM,
    Win5OperationORM,
    Win5RaceORM,
    Win5ResultORM,
    Win5RoundORM,
    Win5ScoreEventORM,
    Win5SeasonORM,
)
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)
from .win5_season_marker import validate_win5_season_marker


class SqlAlchemyWin5SpecialVoidRepository:
    """Apply one Race-void change through an active Session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_round(self, *, round_id: int) -> Win5SpecialVoidRoundTarget | None:
        round_ = self._session.scalar(select(Win5RoundORM).where(Win5RoundORM.id == round_id).with_for_update())
        if round_ is None:
            return None
        season = self._session.get(Win5SeasonORM, round_.season_id)
        if season is None:
            raise RuntimeError("WIN5 Round references a missing Season.")
        season_status = validate_win5_season_marker(
            status=season.status,
            active_marker=season.active_marker,
        )
        return Win5SpecialVoidRoundTarget(
            id=round_.id,
            season_id=round_.season_id,
            type=Win5RoundType(round_.type),
            status=Win5RoundStatus(round_.status),
            season_status=season_status,
            source_kind=Win5RoundSourceKind(round_.source_kind),
        )

    def find_operation(self, *, idempotency_key: str) -> StoredWin5SpecialVoidOperation | None:
        row = self._session.execute(
            select(
                OperationORM.request_fingerprint,
                Win5OperationORM.type,
                Win5OperationORM.round_id,
                Win5OperationORM.after_data,
            )
            .outerjoin(Win5OperationORM, Win5OperationORM.operation_id == OperationORM.id)
            .where(OperationORM.idempotency_key == idempotency_key)
            .with_for_update()
        ).one_or_none()
        if row is None:
            return None
        return StoredWin5SpecialVoidOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            round_id=row.round_id,
            after_data=row.after_data,
        )

    def lock_state(self, *, round_: Win5SpecialVoidRoundTarget) -> Win5SpecialVoidStateSnapshot:
        race_rows = tuple(
            self._session.scalars(
                select(Win5RaceORM).where(Win5RaceORM.round_id == round_.id).order_by(Win5RaceORM.id).with_for_update()
            )
        )
        voids: list[Win5SpecialRaceVoidFact] = []
        for race in race_rows:
            if (race.void_reason is None) != (race.voided_at is None):
                raise ValueError("Special Race has an incomplete current void fact.")
            if race.void_reason is not None and race.voided_at is not None:
                voids.append(
                    Win5SpecialRaceVoidFact(
                        race_id=race.id,
                        reason=race.void_reason,
                        voided_at=from_database_utc(
                            race.voided_at,
                            field_name="win5_races.voided_at",
                        ),
                    )
                )

        result_rows = tuple(
            self._session.scalars(
                select(Win5ResultORM)
                .join(Win5RaceORM, Win5RaceORM.id == Win5ResultORM.race_id)
                .where(Win5RaceORM.round_id == round_.id)
                .order_by(Win5ResultORM.race_id, Win5ResultORM.id)
                .with_for_update()
            )
        )
        results: list[Win5SpecialVoidResultSnapshot] = []
        for result in result_rows:
            if result.position != 1 or result.race_entry_id is not None or result.gate_number is None:
                raise ValueError("Special void state contains a non-Special Result row.")
            results.append(
                Win5SpecialVoidResultSnapshot(
                    id=result.id,
                    race_id=result.race_id,
                    gate_number=result.gate_number,
                )
            )
        return Win5SpecialVoidStateSnapshot(
            season_id=round_.season_id,
            round_id=round_.id,
            round_status=round_.status,
            race_ids=tuple(race.id for race in race_rows),
            voids=tuple(voids),
            results=tuple(results),
        )

    def has_score_events(self, *, round_id: int) -> bool:
        return (
            self._session.scalar(select(Win5ScoreEventORM.id).where(Win5ScoreEventORM.round_id == round_id).limit(1))
            is not None
        )

    def clear_results(self, *, results: tuple[Win5SpecialVoidResultSnapshot, ...]) -> None:
        if not results:
            return
        result_ids = tuple(result.id for result in results)
        deleted = self._session.execute(delete(Win5ResultORM).where(Win5ResultORM.id.in_(result_ids)))
        if deleted.rowcount != len(result_ids):
            raise RuntimeError("Special void mutation could not clear the locked Result bundle.")
        self._session.flush()

    def update_race_void(
        self,
        *,
        race_id: int,
        expected_voided: bool,
        target_voided: bool,
        reason: str,
        changed_at: datetime,
    ) -> None:
        stored_changed_at = to_database_utc(changed_at, field_name="changed_at")
        expected_predicates = (
            (Win5RaceORM.void_reason.is_not(None), Win5RaceORM.voided_at.is_not(None))
            if expected_voided
            else (Win5RaceORM.void_reason.is_(None), Win5RaceORM.voided_at.is_(None))
        )
        changed = self._session.execute(
            update(Win5RaceORM)
            .where(Win5RaceORM.id == race_id, *expected_predicates)
            .values(
                void_reason=reason if target_voided else None,
                voided_at=stored_changed_at if target_voided else None,
                updated_at=stored_changed_at,
            )
        )
        if changed.rowcount != 1:
            raise RuntimeError("Special Race void state changed after it was locked.")
        self._session.flush()

    def void_races(
        self,
        *,
        race_ids: tuple[int, ...],
        reason: str,
        changed_at: datetime,
    ) -> None:
        if not race_ids:
            raise ValueError("Whole-Round cancellation requires one or more non-void Races.")
        stored_changed_at = to_database_utc(changed_at, field_name="changed_at")
        changed = self._session.execute(
            update(Win5RaceORM)
            .where(
                Win5RaceORM.id.in_(race_ids),
                Win5RaceORM.void_reason.is_(None),
                Win5RaceORM.voided_at.is_(None),
            )
            .values(
                void_reason=reason,
                voided_at=stored_changed_at,
                updated_at=stored_changed_at,
            )
        )
        if changed.rowcount != len(race_ids):
            raise RuntimeError("Special Round void state changed after it was locked.")
        self._session.flush()

    def cancel_round(self, *, round_id: int, changed_at: datetime) -> None:
        changed = self._session.execute(
            update(Win5RoundORM)
            .where(
                Win5RoundORM.id == round_id,
                Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
                Win5RoundORM.status == Win5RoundStatus.CLOSED.value,
            )
            .values(
                status=Win5RoundStatus.CANCELLED.value,
                updated_at=to_database_utc(changed_at, field_name="changed_at"),
            )
        )
        if changed.rowcount != 1:
            raise RuntimeError("All-void Special Round changed before cancellation.")
        self._session.flush()

    def add_void_audit(
        self,
        *,
        command: SetSpecialWin5RaceVoid,
        record: Win5SpecialVoidAuditRecord,
        created_at: datetime,
    ) -> None:
        self._add_operation_audit(
            command=command,
            operation_type=record.type.value,
            season_id=record.after.state.season_id,
            round_id=record.after.state.round_id,
            before_data=record.before_data,
            after_data=record.after_data,
            created_at=created_at,
        )

    def add_round_cancellation_audit(
        self,
        *,
        command: CancelSpecialWin5Round,
        record: Win5SpecialRoundCancellationAuditRecord,
        created_at: datetime,
    ) -> None:
        self._add_operation_audit(
            command=command,
            operation_type=record.after.operation_type.value,
            season_id=record.after.state.season_id,
            round_id=record.after.state.round_id,
            before_data=record.before_data,
            after_data=record.after_data,
            created_at=created_at,
        )

    def _add_operation_audit(
        self,
        *,
        command: SetSpecialWin5RaceVoid | CancelSpecialWin5Round,
        operation_type: str,
        season_id: int,
        round_id: int,
        before_data: dict[str, object],
        after_data: dict[str, object],
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
                season_id=season_id,
                round_id=round_id,
                submission_id=None,
                type=operation_type,
                before_data=before_data,
                after_data=after_data,
            )
        )
        self._session.flush()


class SqlAlchemyWin5SpecialVoidUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete feature UoW exposing only Special void persistence."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._win5_special_voids: SqlAlchemyWin5SpecialVoidRepository | None = None

    @property
    def win5_special_voids(self) -> SqlAlchemyWin5SpecialVoidRepository:
        return self._require_active_repository(self._win5_special_voids)

    def _activate_repositories(self) -> None:
        self._win5_special_voids = SqlAlchemyWin5SpecialVoidRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._win5_special_voids = None


class SqlAlchemyWin5SpecialVoidUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyWin5SpecialVoidUnitOfWork]
):
    """Create one Special void UoW per application command."""

    unit_of_work_type = SqlAlchemyWin5SpecialVoidUnitOfWork
