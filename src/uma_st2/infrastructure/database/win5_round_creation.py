"""SQLAlchemy implementation of WIN5 Round creation commands."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from uma_st2.application.win5.round_creation import (
    CreateWin5Round,
    StoredWin5RoundCreationOperation,
    Win5CreatedEntry,
    Win5CreatedRace,
    Win5CreatedRoundGraph,
    Win5RoundCreationAuditRecord,
    Win5RoundCreationSeason,
)
from uma_st2.domain.win5 import Win5RoundSourceKind, Win5RoundStatus

from .datetime_codec import to_database_utc
from .orm import (
    OperationORM,
    Win5OperationORM,
    Win5RaceEntryORM,
    Win5RaceORM,
    Win5RoundORM,
    Win5SeasonORM,
)
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)
from .win5_season_marker import validate_win5_season_marker


class SqlAlchemyWin5RoundCreationRepository:
    """Create one Round/Race graph and its operation audit in one Session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_season(self, *, season_id: int) -> Win5RoundCreationSeason | None:
        season = self._session.scalar(select(Win5SeasonORM).where(Win5SeasonORM.id == season_id).with_for_update())
        if season is None:
            return None
        return Win5RoundCreationSeason(
            id=season.id,
            name=season.name,
            status=validate_win5_season_marker(
                status=season.status,
                active_marker=season.active_marker,
            ),
        )

    def find_operation(self, *, idempotency_key: str) -> StoredWin5RoundCreationOperation | None:
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
        return StoredWin5RoundCreationOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            season_id=row.season_id,
            round_id=row.round_id,
            after_data=row.after_data,
        )

    def create_round_graph(
        self,
        *,
        command: CreateWin5Round,
        created_at: datetime,
    ) -> Win5CreatedRoundGraph:
        stored_created_at = to_database_utc(created_at, field_name="created_at")
        round_ = Win5RoundORM(
            season_id=command.season_id,
            source_kind=Win5RoundSourceKind.NATIVE_V2.value,
            type=command.round_type.value,
            status=Win5RoundStatus.SETUP.value,
            name=command.round_name,
            opens_at=None,
            closes_at=None,
            created_at=stored_created_at,
            updated_at=stored_created_at,
        )
        self._session.add(round_)
        self._session.flush()

        races = [
            Win5RaceORM(
                round_id=round_.id,
                name=race.name,
                scheduled_at=(
                    None if race.scheduled_at is None else to_database_utc(race.scheduled_at, field_name="scheduled_at")
                ),
                created_at=stored_created_at,
                updated_at=stored_created_at,
            )
            for race in command.races
        ]
        self._session.add_all(races)
        self._session.flush()

        entries_by_race: list[tuple[Win5RaceEntryORM, ...]] = []
        for race, requested_race in zip(races, command.races, strict=True):
            entries = tuple(
                Win5RaceEntryORM(
                    race_id=race.id,
                    gate_number=requested_entry.gate_number,
                    name=requested_entry.name,
                    created_at=stored_created_at,
                    updated_at=stored_created_at,
                )
                for requested_entry in requested_race.entries
            )
            self._session.add_all(entries)
            entries_by_race.append(entries)
        self._session.flush()

        return Win5CreatedRoundGraph(
            round_id=round_.id,
            races=tuple(
                Win5CreatedRace(
                    id=race.id,
                    name=requested.name,
                    scheduled_at=requested.scheduled_at,
                    entries=tuple(
                        Win5CreatedEntry(
                            id=entry.id,
                            gate_number=requested_entry.gate_number,
                            name=requested_entry.name,
                        )
                        for entry, requested_entry in zip(
                            entries,
                            requested.entries,
                            strict=True,
                        )
                    ),
                )
                for race, requested, entries in zip(
                    races,
                    command.races,
                    entries_by_race,
                    strict=True,
                )
            ),
        )

    def add_creation_audit(
        self,
        *,
        command: CreateWin5Round,
        record: Win5RoundCreationAuditRecord,
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
                season_id=record.after.season_id,
                round_id=record.after.round_id,
                submission_id=None,
                type=record.type.value,
                before_data=None,
                after_data=record.after_data,
            )
        )
        self._session.flush()


class SqlAlchemyWin5RoundCreationUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete feature UoW exposing only its Round creation repository."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._win5_round_creation: SqlAlchemyWin5RoundCreationRepository | None = None

    @property
    def win5_round_creation(self) -> SqlAlchemyWin5RoundCreationRepository:
        return self._require_active_repository(self._win5_round_creation)

    def _activate_repositories(self) -> None:
        self._win5_round_creation = SqlAlchemyWin5RoundCreationRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._win5_round_creation = None


class SqlAlchemyWin5RoundCreationUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyWin5RoundCreationUnitOfWork]
):
    """Create one Round creation UoW per application command."""

    unit_of_work_type = SqlAlchemyWin5RoundCreationUnitOfWork
