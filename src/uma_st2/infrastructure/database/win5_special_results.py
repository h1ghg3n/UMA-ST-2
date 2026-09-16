"""SQLAlchemy implementation of Special WIN5 result entry and correction."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from uma_st2.application.win5.special_results import (
    SaveSpecialWin5Result,
    StoredWin5SpecialResultOperation,
    Win5SpecialResultAuditRecord,
    Win5SpecialResultRaceTarget,
    Win5SpecialResultRoundTarget,
    Win5SpecialResultWinnerInput,
)
from uma_st2.domain.win5 import (
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
    Win5SpecialResultWinner,
)

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


class SqlAlchemyWin5SpecialResultRepository:
    """Apply one Special result mutation through an active Session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_round(self, *, round_id: int) -> Win5SpecialResultRoundTarget | None:
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
        return Win5SpecialResultRoundTarget(
            id=round_.id,
            season_id=round_.season_id,
            name=round_.name,
            type=Win5RoundType(round_.type),
            status=Win5RoundStatus(round_.status),
            season_status=season_status,
            source_kind=Win5RoundSourceKind(round_.source_kind),
        )

    def find_operation(self, *, idempotency_key: str) -> StoredWin5SpecialResultOperation | None:
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
        return StoredWin5SpecialResultOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            round_id=row.round_id,
            after_data=row.after_data,
        )

    def lock_round_races(self, *, round_id: int) -> tuple[Win5SpecialResultRaceTarget, ...]:
        races: list[Win5SpecialResultRaceTarget] = []
        for race in self._session.scalars(
            select(Win5RaceORM).where(Win5RaceORM.round_id == round_id).order_by(Win5RaceORM.id).with_for_update()
        ):
            if (race.void_reason is None) != (race.voided_at is None):
                raise ValueError("Special Race has an incomplete current void fact.")
            races.append(
                Win5SpecialResultRaceTarget(
                    id=race.id,
                    name=race.name,
                    void_reason=race.void_reason,
                    voided_at=(
                        None
                        if race.voided_at is None
                        else from_database_utc(
                            race.voided_at,
                            field_name="win5_races.voided_at",
                        )
                    ),
                )
            )
        return tuple(races)

    def lock_current_results(self, *, round_id: int) -> tuple[Win5SpecialResultWinner, ...]:
        rows = self._session.execute(
            select(
                Win5ResultORM.id,
                Win5ResultORM.race_id,
                Win5ResultORM.position,
                Win5ResultORM.race_entry_id,
                Win5ResultORM.gate_number,
            )
            .join(Win5RaceORM, Win5RaceORM.id == Win5ResultORM.race_id)
            .where(Win5RaceORM.round_id == round_id)
            .order_by(Win5ResultORM.race_id, Win5ResultORM.id)
            .with_for_update()
        )
        winners: list[Win5SpecialResultWinner] = []
        for row in rows:
            if row.position != 1 or row.race_entry_id is not None or row.gate_number is None:
                raise ValueError("Special Results require position 1, gate_number, and no race_entry_id.")
            winners.append(
                Win5SpecialResultWinner(
                    id=row.id,
                    race_id=row.race_id,
                    gate_number=row.gate_number,
                )
            )
        return tuple(winners)

    def has_score_events(self, *, round_id: int) -> bool:
        return (
            self._session.scalar(select(Win5ScoreEventORM.id).where(Win5ScoreEventORM.round_id == round_id).limit(1))
            is not None
        )

    def create_results(
        self,
        *,
        winners: tuple[Win5SpecialResultWinnerInput, ...],
        created_at: datetime,
    ) -> tuple[Win5SpecialResultWinner, ...]:
        stored_created_at = to_database_utc(created_at, field_name="created_at")
        rows = tuple(
            Win5ResultORM(
                race_id=winner.race_id,
                race_entry_id=None,
                gate_number=winner.gate_number,
                position=1,
                created_at=stored_created_at,
            )
            for winner in winners
        )
        self._session.add_all(rows)
        self._session.flush()
        return tuple(
            Win5SpecialResultWinner(
                id=row.id,
                race_id=row.race_id,
                gate_number=row.gate_number,
            )
            for row in sorted(rows, key=lambda row: row.race_id)
        )

    def replace_results(self, *, winners: tuple[Win5SpecialResultWinner, ...]) -> None:
        for winner in sorted(winners, key=lambda item: item.race_id):
            changed = self._session.execute(
                update(Win5ResultORM)
                .where(
                    Win5ResultORM.id == winner.id,
                    Win5ResultORM.race_id == winner.race_id,
                    Win5ResultORM.position == 1,
                    Win5ResultORM.race_entry_id.is_(None),
                    Win5ResultORM.gate_number.is_not(None),
                )
                .values(gate_number=winner.gate_number)
            )
            if changed.rowcount != 1:
                raise RuntimeError("Special result correction could not preserve its Result row identity.")
        self._session.flush()

    def add_result_audit(
        self,
        *,
        command: SaveSpecialWin5Result,
        round_: Win5SpecialResultRoundTarget,
        record: Win5SpecialResultAuditRecord,
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
                season_id=round_.season_id,
                round_id=round_.id,
                submission_id=None,
                type=record.type.value,
                before_data=record.before_data,
                after_data=record.after_data,
            )
        )
        self._session.flush()


class SqlAlchemyWin5SpecialResultUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete feature UoW exposing only its Special result repository."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._win5_special_results: SqlAlchemyWin5SpecialResultRepository | None = None

    @property
    def win5_special_results(self) -> SqlAlchemyWin5SpecialResultRepository:
        return self._require_active_repository(self._win5_special_results)

    def _activate_repositories(self) -> None:
        self._win5_special_results = SqlAlchemyWin5SpecialResultRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._win5_special_results = None


class SqlAlchemyWin5SpecialResultUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyWin5SpecialResultUnitOfWork]
):
    """Create one Special result UoW per application command."""

    unit_of_work_type = SqlAlchemyWin5SpecialResultUnitOfWork
