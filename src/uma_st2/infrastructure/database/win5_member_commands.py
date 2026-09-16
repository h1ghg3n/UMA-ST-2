"""SQLAlchemy implementation of member-facing WIN5 mutations."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from uma_st2.application.win5.member_commands import Win5RoundMutationTarget, Win5SubmissionMutationTarget
from uma_st2.application.win5.member_identity import Win5MemberPersona
from uma_st2.application.win5.submission_audit import (
    Win5SubmissionAuditRecord,
    Win5SubmissionAuditSnapshot,
    Win5SubmissionPickAuditSnapshot,
)
from uma_st2.domain.win5 import (
    Win5Race,
    Win5RaceEntry,
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
    Win5SubmissionStatus,
    Win5SubmissionTier,
)

from .datetime_codec import to_database_utc
from .orm import (
    OperationORM,
    Win5OperationORM,
    Win5RaceEntryORM,
    Win5RaceORM,
    Win5RoundORM,
    Win5SeasonORM,
    Win5SubmissionORM,
    Win5SubmissionPickORM,
)
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)
from .win5_member_identity import lock_win5_member_persona
from .win5_season_marker import validate_win5_season_marker


class SqlAlchemyWin5MemberCommandRepository:
    """Apply member WIN5 mutations through one active Session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_round(self, *, round_id: int) -> Win5RoundMutationTarget | None:
        """Lock the Round authority row before reading mutable dependencies."""

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

        return Win5RoundMutationTarget(
            id=round_.id,
            season_id=round_.season_id,
            name=round_.name,
            type=Win5RoundType(round_.type),
            season_status=season_status,
            status=Win5RoundStatus(round_.status),
            source_kind=Win5RoundSourceKind(round_.source_kind),
        )

    def lock_member_persona(self, *, discord_user_id: str) -> Win5MemberPersona | None:
        return lock_win5_member_persona(self._session, discord_user_id=discord_user_id)

    def load_round_races(
        self,
        *,
        round_id: int,
        lock_race_ids: tuple[int, ...],
    ) -> tuple[Win5Race, ...]:
        """Load the Round graph and lock addressed Special Race rows in ID order."""

        ordered_lock_ids = tuple(sorted(set(lock_race_ids)))
        if ordered_lock_ids:
            tuple(
                self._session.scalars(
                    select(Win5RaceORM.id)
                    .where(
                        Win5RaceORM.round_id == round_id,
                        Win5RaceORM.id.in_(ordered_lock_ids),
                    )
                    .order_by(Win5RaceORM.id)
                    .with_for_update()
                )
            )

        races = tuple(
            self._session.execute(
                select(Win5RaceORM.id, Win5RaceORM.name)
                .where(Win5RaceORM.round_id == round_id)
                .order_by(Win5RaceORM.id)
            )
        )
        if not races:
            return ()

        entries_by_race: dict[int, list[Win5RaceEntry]] = {race.id: [] for race in races}
        for entry in self._session.execute(
            select(
                Win5RaceEntryORM.id,
                Win5RaceEntryORM.race_id,
                Win5RaceEntryORM.gate_number,
                Win5RaceEntryORM.name,
            )
            .where(Win5RaceEntryORM.race_id.in_(tuple(entries_by_race)))
            .order_by(
                Win5RaceEntryORM.race_id,
                Win5RaceEntryORM.gate_number,
                Win5RaceEntryORM.id,
            )
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

    def lock_accepted_submission(
        self,
        *,
        round_id: int,
        persona_id: str,
    ) -> Win5SubmissionMutationTarget | None:
        submission = self._session.scalar(
            select(Win5SubmissionORM)
            .where(
                Win5SubmissionORM.round_id == round_id,
                Win5SubmissionORM.persona_id == persona_id,
                Win5SubmissionORM.status == Win5SubmissionStatus.ACCEPTED.value,
                Win5SubmissionORM.active_marker.is_(True),
            )
            .with_for_update()
        )
        return None if submission is None else self._submission_target(submission)

    def lock_submission(
        self,
        *,
        submission_id: int,
        round_id: int,
        persona_id: str,
    ) -> Win5SubmissionMutationTarget | None:
        submission = self._session.scalar(
            select(Win5SubmissionORM)
            .where(
                Win5SubmissionORM.id == submission_id,
                Win5SubmissionORM.round_id == round_id,
                Win5SubmissionORM.persona_id == persona_id,
            )
            .with_for_update()
        )
        if submission is None:
            return None

        return self._submission_target(submission)

    def _submission_target(self, submission: Win5SubmissionORM) -> Win5SubmissionMutationTarget:
        picks = tuple(
            Win5SubmissionPickAuditSnapshot(
                race_id=pick.race_id,
                position=pick.position,
                race_entry_id=pick.race_entry_id,
                gate_number=pick.gate_number,
            )
            for pick in self._session.scalars(
                select(Win5SubmissionPickORM)
                .where(Win5SubmissionPickORM.submission_id == submission.id)
                .order_by(
                    Win5SubmissionPickORM.race_id,
                    Win5SubmissionPickORM.position,
                    Win5SubmissionPickORM.id,
                )
            )
        )
        return Win5SubmissionMutationTarget(
            id=submission.id,
            round_id=submission.round_id,
            active_marker=submission.active_marker,
            snapshot=Win5SubmissionAuditSnapshot(
                persona_id=submission.persona_id,
                tier=Win5SubmissionTier(submission.tier),
                status=Win5SubmissionStatus(submission.status),
                version=submission.version,
                picks=picks,
            ),
        )

    def cancel_submission(
        self,
        *,
        submission_id: int,
        expected_version: int,
        updated_at: datetime,
    ) -> bool:
        stored_updated_at = to_database_utc(updated_at, field_name="updated_at")
        result = self._session.execute(
            update(Win5SubmissionORM)
            .where(
                Win5SubmissionORM.id == submission_id,
                Win5SubmissionORM.version == expected_version,
                Win5SubmissionORM.status == Win5SubmissionStatus.ACCEPTED.value,
                Win5SubmissionORM.active_marker.is_(True),
            )
            .values(
                status=Win5SubmissionStatus.CANCELLED.value,
                active_marker=None,
                version=expected_version + 1,
                updated_at=stored_updated_at,
            )
        )
        return result.rowcount == 1

    def create_accepted_submission(
        self,
        *,
        round_id: int,
        persona_id: str,
        tier: Win5SubmissionTier,
        picks: tuple[Win5SubmissionPickAuditSnapshot, ...],
        created_at: datetime,
    ) -> int:
        stored_created_at = to_database_utc(created_at, field_name="created_at")
        submission = Win5SubmissionORM(
            round_id=round_id,
            persona_id=persona_id,
            tier=tier.value,
            status=Win5SubmissionStatus.ACCEPTED.value,
            active_marker=True,
            version=1,
            created_at=stored_created_at,
            updated_at=stored_created_at,
        )
        self._session.add(submission)
        self._session.flush()
        self._add_submission_picks(submission_id=submission.id, picks=picks)
        return submission.id

    def update_accepted_submission(
        self,
        *,
        submission_id: int,
        expected_version: int,
        tier: Win5SubmissionTier,
        picks: tuple[Win5SubmissionPickAuditSnapshot, ...],
        updated_at: datetime,
    ) -> bool:
        stored_updated_at = to_database_utc(updated_at, field_name="updated_at")
        result = self._session.execute(
            update(Win5SubmissionORM)
            .where(
                Win5SubmissionORM.id == submission_id,
                Win5SubmissionORM.version == expected_version,
                Win5SubmissionORM.status == Win5SubmissionStatus.ACCEPTED.value,
                Win5SubmissionORM.active_marker.is_(True),
            )
            .values(
                tier=tier.value,
                version=expected_version + 1,
                updated_at=stored_updated_at,
            )
        )
        if result.rowcount != 1:
            return False

        self._session.execute(delete(Win5SubmissionPickORM).where(Win5SubmissionPickORM.submission_id == submission_id))
        self._add_submission_picks(submission_id=submission_id, picks=picks)
        return True

    def _add_submission_picks(
        self,
        *,
        submission_id: int,
        picks: tuple[Win5SubmissionPickAuditSnapshot, ...],
    ) -> None:
        self._session.add_all(
            Win5SubmissionPickORM(
                submission_id=submission_id,
                race_id=pick.race_id,
                race_entry_id=pick.race_entry_id,
                gate_number=pick.gate_number,
                position=pick.position,
            )
            for pick in picks
        )

    def add_submission_audit(
        self,
        *,
        record: Win5SubmissionAuditRecord,
        actor_discord_user_id: str,
        guild_id: str | None,
        correlation_id: str | None,
        reason: str | None,
        created_at: datetime,
    ) -> None:
        stored_created_at = to_database_utc(created_at, field_name="created_at")
        operation = OperationORM(
            guild_id=guild_id,
            correlation_id=correlation_id,
            actor_discord_user_id=actor_discord_user_id,
            reason=reason,
            created_at=stored_created_at,
        )
        self._session.add(operation)
        self._session.flush()
        self._session.add(
            Win5OperationORM(
                operation_id=operation.id,
                season_id=record.context.season_id,
                round_id=record.context.round_id,
                submission_id=record.context.submission_id,
                type=record.type.value,
                before_data=record.before_data,
                after_data=record.after_data,
            )
        )


class SqlAlchemyWin5MemberCommandUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete feature UoW exposing its Session only through a repository."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._win5_member_commands: SqlAlchemyWin5MemberCommandRepository | None = None

    @property
    def win5_member_commands(self) -> SqlAlchemyWin5MemberCommandRepository:
        return self._require_active_repository(self._win5_member_commands)

    def _activate_repositories(self) -> None:
        self._win5_member_commands = SqlAlchemyWin5MemberCommandRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._win5_member_commands = None


class SqlAlchemyWin5MemberCommandUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyWin5MemberCommandUnitOfWork]
):
    """Create one feature command UoW per application operation."""

    unit_of_work_type = SqlAlchemyWin5MemberCommandUnitOfWork
