"""SQLAlchemy implementation of guarded WIN5 setup-Round deletion."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from uma_st2.application.win5.round_deletion import (
    WIN5_ROUND_PUBLICATION_SOURCE_KIND,
    DeleteWin5SetupRound,
    StoredWin5SetupRoundDeletionOperation,
    Win5SetupRoundDeletionAuditRecord,
    Win5SetupRoundDeletionEntry,
    Win5SetupRoundDeletionRace,
    Win5SetupRoundDeletionSeason,
    Win5SetupRoundDeletionSnapshot,
    Win5SetupRoundDependencyState,
)
from uma_st2.domain.win5 import Win5RoundSourceKind, Win5RoundStatus, Win5RoundType

from .datetime_codec import from_database_utc, to_database_utc
from .orm import (
    DiscordPublicationORM,
    OperationORM,
    Win5OperationORM,
    Win5RaceEntryORM,
    Win5RaceORM,
    Win5ResultORM,
    Win5RoundORM,
    Win5ScoreEventORM,
    Win5SeasonORM,
    Win5SubmissionORM,
)
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)
from .win5_season_marker import validate_win5_season_marker


class SqlAlchemyWin5SetupRoundDeletionRepository:
    """Lock, guard, audit, and remove one setup Round graph."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_season(self, *, season_id: int) -> Win5SetupRoundDeletionSeason | None:
        season = self._session.scalar(select(Win5SeasonORM).where(Win5SeasonORM.id == season_id).with_for_update())
        if season is None:
            return None
        return Win5SetupRoundDeletionSeason(
            id=season.id,
            name=season.name,
            status=validate_win5_season_marker(
                status=season.status,
                active_marker=season.active_marker,
            ),
        )

    def find_operation(self, *, idempotency_key: str) -> StoredWin5SetupRoundDeletionOperation | None:
        row = self._session.execute(
            select(
                OperationORM.request_fingerprint,
                Win5OperationORM.type,
                Win5OperationORM.season_id,
                Win5OperationORM.round_id,
                Win5OperationORM.before_data,
                Win5OperationORM.after_data,
            )
            .outerjoin(Win5OperationORM, Win5OperationORM.operation_id == OperationORM.id)
            .where(OperationORM.idempotency_key == idempotency_key)
            .with_for_update()
        ).one_or_none()
        if row is None:
            return None
        return StoredWin5SetupRoundDeletionOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            season_id=row.season_id,
            round_id=row.round_id,
            before_data=row.before_data,
            after_data=row.after_data,
        )

    def lock_round_graph(
        self,
        *,
        season: Win5SetupRoundDeletionSeason,
        round_id: int,
    ) -> Win5SetupRoundDeletionSnapshot | None:
        round_ = self._session.scalar(
            select(Win5RoundORM)
            .where(
                Win5RoundORM.id == round_id,
                Win5RoundORM.season_id == season.id,
            )
            .with_for_update()
        )
        if round_ is None:
            return None
        races = self._session.scalars(
            select(Win5RaceORM).where(Win5RaceORM.round_id == round_id).order_by(Win5RaceORM.id).with_for_update()
        ).all()
        race_ids = tuple(race.id for race in races)
        entries = (
            self._session.scalars(
                select(Win5RaceEntryORM)
                .where(Win5RaceEntryORM.race_id.in_(race_ids))
                .order_by(
                    Win5RaceEntryORM.race_id,
                    Win5RaceEntryORM.gate_number,
                    Win5RaceEntryORM.id,
                )
                .with_for_update()
            ).all()
            if race_ids
            else []
        )
        entries_by_race: defaultdict[int, list[Win5SetupRoundDeletionEntry]] = defaultdict(list)
        for entry in entries:
            entries_by_race[entry.race_id].append(
                Win5SetupRoundDeletionEntry(
                    id=entry.id,
                    gate_number=entry.gate_number,
                    name=entry.name,
                )
            )
        return Win5SetupRoundDeletionSnapshot(
            season_id=season.id,
            season_name=season.name,
            season_status=season.status,
            round_id=round_.id,
            round_name=round_.name,
            round_type=Win5RoundType(round_.type),
            round_status=Win5RoundStatus(round_.status),
            source_kind=Win5RoundSourceKind(round_.source_kind),
            opens_at=(
                None
                if round_.opens_at is None
                else from_database_utc(round_.opens_at, field_name="win5_rounds.opens_at")
            ),
            closes_at=(
                None
                if round_.closes_at is None
                else from_database_utc(round_.closes_at, field_name="win5_rounds.closes_at")
            ),
            races=tuple(
                Win5SetupRoundDeletionRace(
                    id=race.id,
                    name=race.name,
                    scheduled_at=(
                        None
                        if race.scheduled_at is None
                        else from_database_utc(race.scheduled_at, field_name="win5_races.scheduled_at")
                    ),
                    entries=tuple(entries_by_race[race.id]),
                )
                for race in races
            ),
        )

    def lock_dependency_state(
        self,
        *,
        round_id: int,
        race_ids: tuple[int, ...],
    ) -> Win5SetupRoundDependencyState:
        submission_ids = self._session.scalars(
            select(Win5SubmissionORM.id)
            .where(Win5SubmissionORM.round_id == round_id)
            .order_by(Win5SubmissionORM.id)
            .with_for_update()
        ).all()
        result_ids = (
            self._session.scalars(
                select(Win5ResultORM.id)
                .where(Win5ResultORM.race_id.in_(race_ids))
                .order_by(Win5ResultORM.id)
                .with_for_update()
            ).all()
            if race_ids
            else []
        )
        score_event_ids = self._session.scalars(
            select(Win5ScoreEventORM.id)
            .where(Win5ScoreEventORM.round_id == round_id)
            .order_by(Win5ScoreEventORM.id)
            .with_for_update()
        ).all()
        publication_ids = self._session.scalars(
            select(DiscordPublicationORM.id)
            .where(
                DiscordPublicationORM.source_kind == WIN5_ROUND_PUBLICATION_SOURCE_KIND,
                DiscordPublicationORM.source_id == round_id,
            )
            .order_by(DiscordPublicationORM.id)
            .with_for_update()
        ).all()
        return Win5SetupRoundDependencyState(
            submission_count=len(submission_ids),
            result_count=len(result_ids),
            score_event_count=len(score_event_ids),
            publication_count=len(publication_ids),
        )

    def delete_round_graph(self, *, snapshot: Win5SetupRoundDeletionSnapshot) -> None:
        race_ids = tuple(race.id for race in snapshot.races)
        if race_ids:
            deleted_entries = self._session.execute(
                delete(Win5RaceEntryORM).where(Win5RaceEntryORM.race_id.in_(race_ids))
            )
            if deleted_entries.rowcount != snapshot.entry_count:
                raise RuntimeError("WIN5 Entry graph changed while setup Round deletion was being applied.")
            deleted_races = self._session.execute(
                delete(Win5RaceORM).where(
                    Win5RaceORM.round_id == snapshot.round_id,
                    Win5RaceORM.id.in_(race_ids),
                )
            )
            if deleted_races.rowcount != snapshot.race_count:
                raise RuntimeError("WIN5 Race graph changed while setup Round deletion was being applied.")
        deleted_round = self._session.execute(
            delete(Win5RoundORM).where(
                Win5RoundORM.id == snapshot.round_id,
                Win5RoundORM.season_id == snapshot.season_id,
                Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
                Win5RoundORM.status == Win5RoundStatus.SETUP.value,
            )
        )
        if deleted_round.rowcount != 1:
            raise RuntimeError("WIN5 Round changed while setup Round deletion was being applied.")
        self._session.flush()

    def add_deletion_audit(
        self,
        *,
        command: DeleteWin5SetupRound,
        record: Win5SetupRoundDeletionAuditRecord,
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
                season_id=record.before.season_id,
                round_id=record.before.round_id,
                submission_id=None,
                type=record.type.value,
                before_data=record.before_data,
                after_data=record.after_data,
            )
        )
        self._session.flush()


class SqlAlchemyWin5SetupRoundDeletionUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete feature UoW exposing only setup-Round deletion."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._repository: SqlAlchemyWin5SetupRoundDeletionRepository | None = None

    @property
    def win5_setup_round_deletion(self) -> SqlAlchemyWin5SetupRoundDeletionRepository:
        return self._require_active_repository(self._repository)

    def _activate_repositories(self) -> None:
        self._repository = SqlAlchemyWin5SetupRoundDeletionRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._repository = None


class SqlAlchemyWin5SetupRoundDeletionUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyWin5SetupRoundDeletionUnitOfWork]
):
    """Create one setup-Round deletion UoW per application command."""

    unit_of_work_type = SqlAlchemyWin5SetupRoundDeletionUnitOfWork
