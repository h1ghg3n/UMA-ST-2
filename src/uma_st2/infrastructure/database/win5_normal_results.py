"""SQLAlchemy implementation of Normal WIN5 result entry and correction."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from uma_st2.application.win5.normal_results import (
    SaveNormalWin5Result,
    StoredWin5NormalResultOperation,
    Win5NormalResultAuditRecord,
    Win5NormalResultPlacementInput,
    Win5NormalResultRoundTarget,
)
from uma_st2.domain.win5 import (
    Win5NormalResultPlacement,
    Win5Race,
    Win5RaceEntry,
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
)

from .datetime_codec import to_database_utc
from .orm import (
    OperationORM,
    Win5OperationORM,
    Win5RaceEntryORM,
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


class SqlAlchemyWin5NormalResultRepository:
    """Apply one Normal result mutation through an active Session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_round(self, *, round_id: int) -> Win5NormalResultRoundTarget | None:
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
        return Win5NormalResultRoundTarget(
            id=round_.id,
            season_id=round_.season_id,
            name=round_.name,
            type=Win5RoundType(round_.type),
            status=Win5RoundStatus(round_.status),
            season_status=season_status,
            source_kind=Win5RoundSourceKind(round_.source_kind),
        )

    def find_operation(self, *, idempotency_key: str) -> StoredWin5NormalResultOperation | None:
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
        return StoredWin5NormalResultOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            round_id=row.round_id,
            after_data=row.after_data,
        )

    def lock_round_races(self, *, round_id: int) -> tuple[Win5Race, ...]:
        races = tuple(
            self._session.scalars(
                select(Win5RaceORM).where(Win5RaceORM.round_id == round_id).order_by(Win5RaceORM.id).with_for_update()
            )
        )
        if not races:
            return ()
        entries_by_race: dict[int, list[Win5RaceEntry]] = {race.id: [] for race in races}
        for entry in self._session.scalars(
            select(Win5RaceEntryORM)
            .where(Win5RaceEntryORM.race_id.in_(tuple(entries_by_race)))
            .order_by(
                Win5RaceEntryORM.race_id,
                Win5RaceEntryORM.gate_number,
                Win5RaceEntryORM.id,
            )
            .with_for_update()
        ):
            entries_by_race[entry.race_id].append(
                Win5RaceEntry(
                    id=entry.id,
                    race_id=entry.race_id,
                    gate_number=entry.gate_number,
                    name=entry.name,
                )
            )
        return tuple(
            Win5Race(
                id=race.id,
                name=race.name,
                entries=tuple(entries_by_race[race.id]),
            )
            for race in races
        )

    def lock_current_result(self, *, race_id: int) -> tuple[Win5NormalResultPlacement, ...]:
        rows = self._session.execute(
            select(
                Win5ResultORM.id,
                Win5ResultORM.position,
                Win5ResultORM.race_entry_id,
                Win5ResultORM.gate_number,
            )
            .where(Win5ResultORM.race_id == race_id)
            .order_by(Win5ResultORM.position, Win5ResultORM.id)
            .with_for_update()
        )
        placements: list[Win5NormalResultPlacement] = []
        for row in rows:
            if row.race_entry_id is None or row.gate_number is not None:
                raise ValueError("Normal Results require race_entry_id and no gate_number.")
            placements.append(
                Win5NormalResultPlacement(
                    id=row.id,
                    position=row.position,
                    race_entry_id=row.race_entry_id,
                )
            )
        return tuple(placements)

    def has_score_events(self, *, round_id: int) -> bool:
        return (
            self._session.scalar(select(Win5ScoreEventORM.id).where(Win5ScoreEventORM.round_id == round_id).limit(1))
            is not None
        )

    def create_result(
        self,
        *,
        race_id: int,
        placements: tuple[Win5NormalResultPlacementInput, ...],
        created_at: datetime,
    ) -> tuple[Win5NormalResultPlacement, ...]:
        stored_created_at = to_database_utc(created_at, field_name="created_at")
        rows = tuple(
            Win5ResultORM(
                race_id=race_id,
                race_entry_id=placement.race_entry_id,
                gate_number=None,
                position=placement.position,
                created_at=stored_created_at,
            )
            for placement in placements
        )
        self._session.add_all(rows)
        self._session.flush()
        return tuple(
            Win5NormalResultPlacement(
                id=row.id,
                position=row.position,
                race_entry_id=row.race_entry_id,
            )
            for row in sorted(rows, key=lambda row: row.position)
        )

    def replace_result(
        self,
        *,
        race_id: int,
        placements: tuple[Win5NormalResultPlacement, ...],
    ) -> None:
        cleared = self._session.execute(
            update(Win5ResultORM).where(Win5ResultORM.race_id == race_id).values(race_entry_id=None, gate_number=None)
        )
        if cleared.rowcount != 5:
            raise RuntimeError("Normal result correction lost its complete current board.")

        for placement in sorted(placements, key=lambda item: item.position):
            changed = self._session.execute(
                update(Win5ResultORM)
                .where(
                    Win5ResultORM.id == placement.id,
                    Win5ResultORM.race_id == race_id,
                    Win5ResultORM.position == placement.position,
                    Win5ResultORM.race_entry_id.is_(None),
                    Win5ResultORM.gate_number.is_(None),
                )
                .values(race_entry_id=placement.race_entry_id)
            )
            if changed.rowcount != 1:
                raise RuntimeError("Normal result correction could not preserve its Result row identity.")
        self._session.flush()

    def add_result_audit(
        self,
        *,
        command: SaveNormalWin5Result,
        round_: Win5NormalResultRoundTarget,
        record: Win5NormalResultAuditRecord,
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


class SqlAlchemyWin5NormalResultUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete feature UoW exposing only its Normal result repository."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._win5_normal_results: SqlAlchemyWin5NormalResultRepository | None = None

    @property
    def win5_normal_results(self) -> SqlAlchemyWin5NormalResultRepository:
        return self._require_active_repository(self._win5_normal_results)

    def _activate_repositories(self) -> None:
        self._win5_normal_results = SqlAlchemyWin5NormalResultRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._win5_normal_results = None


class SqlAlchemyWin5NormalResultUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyWin5NormalResultUnitOfWork]
):
    """Create one Normal result UoW per application command."""

    unit_of_work_type = SqlAlchemyWin5NormalResultUnitOfWork
