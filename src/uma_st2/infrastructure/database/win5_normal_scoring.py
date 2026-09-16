"""SQLAlchemy implementation of atomic Normal WIN5 scoring."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from uma_st2.application.publication import Win5RoundPublicationSource
from uma_st2.application.win5.normal_scoring import (
    WIN5_NORMAL_SCORING_OPERATION_TYPE,
    StoredWin5ScoringOperation,
    Win5NormalScoringMutation,
    Win5NormalScoringPickTarget,
    Win5NormalScoringRoundTarget,
    Win5NormalScoringSubmissionTarget,
)
from uma_st2.domain.win5 import (
    WIN5_NORMAL_REWARD_POLICY_VERSION,
    WIN5_NORMAL_SCORING_POLICY_VERSION,
    Win5NormalResultPlacement,
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
    Win5SubmissionStatus,
    Win5SubmissionTier,
)

from .datetime_codec import to_database_utc
from .orm import (
    CirclePointORM,
    OperationORM,
    PointTransactionORM,
    Win5OperationORM,
    Win5RaceORM,
    Win5ResultORM,
    Win5RoundORM,
    Win5ScoreEventItemORM,
    Win5ScoreEventORM,
    Win5ScoreORM,
    Win5SeasonORM,
    Win5SubmissionORM,
    Win5SubmissionPickORM,
)
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)
from .win5_publication import SqlAlchemyWin5PublicationStore
from .win5_season_marker import validate_win5_season_marker

_WIN5_REWARD_ACTION = "win5_reward"


class SqlAlchemyWin5NormalScoringRepository:
    """Apply one Normal scoring batch through an active Session."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._publication_store = SqlAlchemyWin5PublicationStore(session)

    def lock_round(self, *, round_id: int) -> Win5NormalScoringRoundTarget | None:
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
        return Win5NormalScoringRoundTarget(
            id=round_.id,
            season_id=round_.season_id,
            type=Win5RoundType(round_.type),
            status=Win5RoundStatus(round_.status),
            season_status=season_status,
            source_kind=Win5RoundSourceKind(round_.source_kind),
        )

    def find_operation(self, *, idempotency_key: str) -> StoredWin5ScoringOperation | None:
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
        return StoredWin5ScoringOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            round_id=row.round_id,
            after_data=row.after_data,
        )

    def lock_round_race_ids(self, *, round_id: int) -> tuple[int, ...]:
        return tuple(
            self._session.scalars(
                select(Win5RaceORM.id)
                .where(Win5RaceORM.round_id == round_id)
                .order_by(Win5RaceORM.id)
                .with_for_update()
            )
        )

    def lock_result_board(self, *, race_id: int) -> tuple[Win5NormalResultPlacement, ...]:
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
                raise ValueError("Normal scoring Results require race_entry_id and no gate_number.")
            placements.append(
                Win5NormalResultPlacement(
                    id=row.id,
                    position=row.position,
                    race_entry_id=row.race_entry_id,
                )
            )
        return tuple(placements)

    def lock_accepted_submissions(
        self,
        *,
        round_id: int,
    ) -> tuple[Win5NormalScoringSubmissionTarget, ...]:
        submissions = tuple(
            self._session.scalars(
                select(Win5SubmissionORM)
                .where(
                    Win5SubmissionORM.round_id == round_id,
                    Win5SubmissionORM.status == Win5SubmissionStatus.ACCEPTED.value,
                )
                .order_by(Win5SubmissionORM.persona_id, Win5SubmissionORM.id)
                .with_for_update()
            )
        )
        if not submissions:
            return ()

        picks_by_submission: dict[int, list[Win5NormalScoringPickTarget]] = {
            submission.id: [] for submission in submissions
        }
        for pick in self._session.scalars(
            select(Win5SubmissionPickORM)
            .where(Win5SubmissionPickORM.submission_id.in_(tuple(picks_by_submission)))
            .order_by(
                Win5SubmissionPickORM.submission_id,
                Win5SubmissionPickORM.race_id,
                Win5SubmissionPickORM.position,
                Win5SubmissionPickORM.id,
            )
            .with_for_update()
        ):
            if pick.race_entry_id is None or pick.gate_number is not None:
                raise ValueError("Normal scoring picks require race_entry_id and no gate_number.")
            picks_by_submission[pick.submission_id].append(
                Win5NormalScoringPickTarget(
                    id=pick.id,
                    race_id=pick.race_id,
                    position=pick.position,
                    race_entry_id=pick.race_entry_id,
                )
            )

        return tuple(
            Win5NormalScoringSubmissionTarget(
                id=submission.id,
                round_id=submission.round_id,
                persona_id=submission.persona_id,
                tier=Win5SubmissionTier(submission.tier),
                version=submission.version,
                active_marker=submission.active_marker,
                picks=tuple(picks_by_submission[submission.id]),
            )
            for submission in submissions
        )

    def lock_circle_point_wallets(self, *, persona_ids: tuple[str, ...]) -> tuple[str, ...]:
        ordered_persona_ids = tuple(sorted(set(persona_ids)))
        if not ordered_persona_ids:
            return ()
        return tuple(
            self._session.scalars(
                select(CirclePointORM.persona_id)
                .where(CirclePointORM.persona_id.in_(ordered_persona_ids))
                .order_by(CirclePointORM.persona_id)
                .with_for_update()
            )
        )

    def load_scored_round_publication_source(
        self,
        *,
        guild_id: str,
        round_id: int,
        persona_ids: tuple[str, ...],
    ) -> Win5RoundPublicationSource:
        return self._publication_store.load_scored_round_source(
            guild_id=guild_id,
            round_id=round_id,
            persona_ids=persona_ids,
        )

    def apply_scoring(self, *, mutation: Win5NormalScoringMutation) -> None:
        stored_created_at = to_database_utc(mutation.created_at, field_name="created_at")
        operation = OperationORM(
            guild_id=mutation.command.guild_id,
            correlation_id=mutation.command.correlation_id,
            actor_discord_user_id=mutation.command.actor_discord_user_id,
            idempotency_key=mutation.command.idempotency_key,
            request_fingerprint=mutation.command.request_fingerprint,
            reason=mutation.command.reason,
            created_at=stored_created_at,
        )
        self._session.add(operation)
        self._session.flush()
        self._session.add(
            Win5OperationORM(
                operation_id=operation.id,
                season_id=mutation.round.season_id,
                round_id=mutation.round.id,
                submission_id=None,
                type=WIN5_NORMAL_SCORING_OPERATION_TYPE,
                before_data=mutation.before_data,
                after_data=mutation.after_data,
            )
        )

        for event_mutation in mutation.events:
            submission = event_mutation.submission
            score = event_mutation.score
            event = Win5ScoreEventORM(
                operation_id=operation.id,
                season_id=mutation.round.season_id,
                round_id=mutation.round.id,
                race_id=mutation.race_id,
                submission_id=submission.id,
                submission_version=submission.version,
                persona_id=submission.persona_id,
                tier=submission.tier.value,
                result_fingerprint=mutation.result_fingerprint,
                scoring_policy_version=WIN5_NORMAL_SCORING_POLICY_VERSION,
                reward_policy_version=WIN5_NORMAL_REWARD_POLICY_VERSION,
                exact_count=score.exact_count,
                wrong_position_count=score.wrong_position_count,
                off_board_count=score.off_board_count,
                missing_count=score.missing_count,
                season_score_delta=score.season_score_delta,
                top1_score_delta=score.top1_score_delta,
                circle_point_reward=score.circle_point_reward,
                created_at=stored_created_at,
            )
            self._session.add(event)
            self._session.flush()
            self._session.add_all(
                Win5ScoreEventItemORM(
                    score_event_id=event.id,
                    race_id=mutation.race_id,
                    position=item.position,
                    submission_pick_id=item.submission_pick_id,
                    matched_result_id=item.matched_result_id,
                    outcome=item.outcome.value,
                    season_score_delta=item.season_score_delta,
                )
                for item in score.items
            )
            self._upsert_score_projection(
                season_id=mutation.round.season_id,
                persona_id=submission.persona_id,
                season_score_delta=score.season_score_delta,
                top1_score_delta=score.top1_score_delta,
                updated_at=stored_created_at,
            )
            if score.circle_point_reward > 0:
                self._apply_reward(
                    persona_id=submission.persona_id,
                    operation_id=operation.id,
                    amount=score.circle_point_reward,
                    created_at=stored_created_at,
                )

        changed = self._session.execute(
            update(Win5RoundORM)
            .where(
                Win5RoundORM.id == mutation.round.id,
                Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
                Win5RoundORM.status == Win5RoundStatus.CLOSED.value,
            )
            .values(status=Win5RoundStatus.SCORED.value, updated_at=stored_created_at)
        )
        if changed.rowcount != 1:
            raise RuntimeError("WIN5 Round changed before scoring could be finalized.")
        self._publication_store.add_intents(
            intents=mutation.publication_intents,
            created_at=mutation.created_at,
        )
        self._session.flush()

    def _upsert_score_projection(
        self,
        *,
        season_id: int,
        persona_id: str,
        season_score_delta: int,
        top1_score_delta: int,
        updated_at: datetime,
    ) -> None:
        values = {
            "season_id": season_id,
            "persona_id": persona_id,
            "season_score": season_score_delta,
            "top1_score": top1_score_delta,
            "updated_at": updated_at,
        }
        dialect_name = self._session.get_bind().dialect.name
        if dialect_name == "mysql":
            statement = mysql_insert(Win5ScoreORM).values(**values)
            self._session.execute(
                statement.on_duplicate_key_update(
                    season_score=Win5ScoreORM.season_score + statement.inserted.season_score,
                    top1_score=Win5ScoreORM.top1_score + statement.inserted.top1_score,
                    updated_at=statement.inserted.updated_at,
                )
            )
            return
        if dialect_name == "sqlite":
            statement = sqlite_insert(Win5ScoreORM).values(**values)
            self._session.execute(
                statement.on_conflict_do_update(
                    index_elements=[Win5ScoreORM.season_id, Win5ScoreORM.persona_id],
                    set_={
                        "season_score": Win5ScoreORM.season_score + statement.excluded.season_score,
                        "top1_score": Win5ScoreORM.top1_score + statement.excluded.top1_score,
                        "updated_at": statement.excluded.updated_at,
                    },
                )
            )
            return

        projection = self._session.scalar(
            select(Win5ScoreORM)
            .where(
                Win5ScoreORM.season_id == season_id,
                Win5ScoreORM.persona_id == persona_id,
            )
            .with_for_update()
        )
        if projection is None:
            self._session.add(Win5ScoreORM(**values))
        else:
            projection.season_score += season_score_delta
            projection.top1_score += top1_score_delta
            projection.updated_at = updated_at

    def _apply_reward(
        self,
        *,
        persona_id: str,
        operation_id: int,
        amount: int,
        created_at: datetime,
    ) -> None:
        changed = self._session.execute(
            update(CirclePointORM)
            .where(CirclePointORM.persona_id == persona_id)
            .values(
                balance=CirclePointORM.balance + amount,
                updated_at=created_at,
            )
        )
        if changed.rowcount != 1:
            raise RuntimeError("Locked Circle Point wallet disappeared during WIN5 scoring.")
        self._session.add(
            PointTransactionORM(
                persona_id=persona_id,
                operation_id=operation_id,
                action=_WIN5_REWARD_ACTION,
                amount=amount,
                created_at=created_at,
            )
        )


class SqlAlchemyWin5NormalScoringUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete feature UoW exposing only its scoring repository."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._win5_normal_scoring: SqlAlchemyWin5NormalScoringRepository | None = None

    @property
    def win5_normal_scoring(self) -> SqlAlchemyWin5NormalScoringRepository:
        return self._require_active_repository(self._win5_normal_scoring)

    def _activate_repositories(self) -> None:
        self._win5_normal_scoring = SqlAlchemyWin5NormalScoringRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._win5_normal_scoring = None


class SqlAlchemyWin5NormalScoringUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyWin5NormalScoringUnitOfWork]
):
    """Create one Normal scoring UoW per application command."""

    unit_of_work_type = SqlAlchemyWin5NormalScoringUnitOfWork
