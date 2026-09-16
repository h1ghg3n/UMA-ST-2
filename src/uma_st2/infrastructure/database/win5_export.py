"""SQLAlchemy projection reader for complete WIN5 Season exports."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from uma_st2.application.exporting import (
    Win5ExportEntry,
    Win5ExportJudgementItem,
    Win5ExportPick,
    Win5ExportRace,
    Win5ExportResult,
    Win5ExportRound,
    Win5ExportScore,
    Win5ExportScoreEvent,
    Win5ExportSeasonChoice,
    Win5ExportSubmission,
    Win5SeasonExportSource,
)
from uma_st2.domain.win5 import (
    Win5JudgementOutcome,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SubmissionStatus,
    Win5SubmissionTier,
)

from .datetime_codec import from_database_utc
from .orm import (
    PersonaORM,
    Win5RaceEntryORM,
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


def _optional_utc(value: datetime | None, *, field_name: str) -> datetime | None:
    return None if value is None else from_database_utc(value, field_name=field_name)


class SqlAlchemyWin5SeasonExportRepository:
    """Materialize complete detached WIN5 Season export sources."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def search_seasons(self, *, query: str, limit: int) -> tuple[Win5ExportSeasonChoice, ...]:
        statement = select(Win5SeasonORM).where(
            Win5SeasonORM.status.in_((Win5SeasonStatus.ACTIVE.value, Win5SeasonStatus.CLOSED.value))
        )
        if query:
            statement = statement.where(Win5SeasonORM.name.contains(query, autoescape=True))
        active_first = case((Win5SeasonORM.status == Win5SeasonStatus.ACTIVE.value, 0), else_=1)
        seasons = self._session.scalars(
            statement.order_by(active_first, Win5SeasonORM.created_at.desc(), Win5SeasonORM.id.desc()).limit(limit)
        )
        return tuple(
            Win5ExportSeasonChoice(
                id=season.id,
                name=season.name,
                status=Win5SeasonStatus(season.status),
            )
            for season in seasons
        )

    def get_season_source(
        self,
        *,
        season_id: int,
        source_cutoff: datetime,
    ) -> Win5SeasonExportSource | None:
        season = self._session.scalar(
            select(Win5SeasonORM).where(
                Win5SeasonORM.id == season_id,
                Win5SeasonORM.status.in_((Win5SeasonStatus.ACTIVE.value, Win5SeasonStatus.CLOSED.value)),
            )
        )
        if season is None:
            return None

        round_rows = tuple(
            self._session.scalars(
                select(Win5RoundORM)
                .where(Win5RoundORM.season_id == season_id)
                .order_by(Win5RoundORM.created_at, Win5RoundORM.id)
            )
        )
        round_ids = tuple(round_.id for round_ in round_rows)
        race_rows = (
            tuple(
                self._session.scalars(
                    select(Win5RaceORM)
                    .where(Win5RaceORM.round_id.in_(round_ids))
                    .order_by(Win5RaceORM.round_id, Win5RaceORM.id)
                )
            )
            if round_ids
            else ()
        )
        race_ids = tuple(race.id for race in race_rows)
        entry_rows = (
            tuple(
                self._session.scalars(
                    select(Win5RaceEntryORM)
                    .where(Win5RaceEntryORM.race_id.in_(race_ids))
                    .order_by(
                        Win5RaceEntryORM.race_id,
                        Win5RaceEntryORM.gate_number,
                        Win5RaceEntryORM.id,
                    )
                )
            )
            if race_ids
            else ()
        )
        result_rows = (
            tuple(
                self._session.scalars(
                    select(Win5ResultORM)
                    .where(Win5ResultORM.race_id.in_(race_ids))
                    .order_by(
                        Win5ResultORM.race_id,
                        Win5ResultORM.position,
                        Win5ResultORM.id,
                    )
                )
            )
            if race_ids
            else ()
        )

        submission_rows = (
            tuple(
                self._session.execute(
                    select(Win5SubmissionORM, PersonaORM.display_name)
                    .join(PersonaORM, PersonaORM.id == Win5SubmissionORM.persona_id)
                    .where(Win5SubmissionORM.round_id.in_(round_ids))
                    .order_by(
                        Win5SubmissionORM.round_id,
                        Win5SubmissionORM.created_at,
                        Win5SubmissionORM.id,
                    )
                ).all()
            )
            if round_ids
            else ()
        )
        submission_ids = tuple(submission.id for submission, _ in submission_rows)
        pick_rows = (
            tuple(
                self._session.scalars(
                    select(Win5SubmissionPickORM)
                    .where(Win5SubmissionPickORM.submission_id.in_(submission_ids))
                    .order_by(
                        Win5SubmissionPickORM.submission_id,
                        Win5SubmissionPickORM.race_id,
                        Win5SubmissionPickORM.position,
                        Win5SubmissionPickORM.id,
                    )
                )
            )
            if submission_ids
            else ()
        )
        event_rows = (
            tuple(
                self._session.scalars(
                    select(Win5ScoreEventORM)
                    .where(Win5ScoreEventORM.submission_id.in_(submission_ids))
                    .order_by(Win5ScoreEventORM.submission_id, Win5ScoreEventORM.id)
                )
            )
            if submission_ids
            else ()
        )
        event_ids = tuple(event.id for event in event_rows)
        item_rows = (
            tuple(
                self._session.scalars(
                    select(Win5ScoreEventItemORM)
                    .where(Win5ScoreEventItemORM.score_event_id.in_(event_ids))
                    .order_by(
                        Win5ScoreEventItemORM.score_event_id,
                        Win5ScoreEventItemORM.race_id,
                        Win5ScoreEventItemORM.position,
                        Win5ScoreEventItemORM.id,
                    )
                )
            )
            if event_ids
            else ()
        )
        score_rows = tuple(
            self._session.execute(
                select(Win5ScoreORM, PersonaORM.display_name)
                .join(PersonaORM, PersonaORM.id == Win5ScoreORM.persona_id)
                .where(Win5ScoreORM.season_id == season_id)
                .order_by(Win5ScoreORM.persona_id)
            ).all()
        )

        entries_by_race: dict[int, list[Win5ExportEntry]] = defaultdict(list)
        for entry in entry_rows:
            entries_by_race[entry.race_id].append(
                Win5ExportEntry(
                    id=entry.id,
                    gate_number=entry.gate_number,
                    name=entry.name,
                )
            )
        results_by_race: dict[int, list[Win5ExportResult]] = defaultdict(list)
        for result in result_rows:
            results_by_race[result.race_id].append(
                Win5ExportResult(
                    id=result.id,
                    race_id=result.race_id,
                    position=result.position,
                    race_entry_id=result.race_entry_id,
                    gate_number=result.gate_number,
                )
            )
        races_by_round: dict[int, list[Win5ExportRace]] = defaultdict(list)
        for race in race_rows:
            races_by_round[race.round_id].append(
                Win5ExportRace(
                    id=race.id,
                    name=race.name,
                    scheduled_at=_optional_utc(
                        race.scheduled_at,
                        field_name="win5_races.scheduled_at",
                    ),
                    void_reason=race.void_reason,
                    voided_at=_optional_utc(
                        race.voided_at,
                        field_name="win5_races.voided_at",
                    ),
                    entries=tuple(entries_by_race[race.id]),
                    results=tuple(results_by_race[race.id]),
                )
            )
        rounds = tuple(
            Win5ExportRound(
                id=round_.id,
                round_type=Win5RoundType(round_.type),
                status=Win5RoundStatus(round_.status),
                name=round_.name,
                opens_at=_optional_utc(
                    round_.opens_at,
                    field_name="win5_rounds.opens_at",
                ),
                closes_at=_optional_utc(
                    round_.closes_at,
                    field_name="win5_rounds.closes_at",
                ),
                races=tuple(races_by_round[round_.id]),
            )
            for round_ in round_rows
        )

        picks_by_submission: dict[int, list[Win5ExportPick]] = defaultdict(list)
        for pick in pick_rows:
            picks_by_submission[pick.submission_id].append(
                Win5ExportPick(
                    id=pick.id,
                    race_id=pick.race_id,
                    position=pick.position,
                    race_entry_id=pick.race_entry_id,
                    gate_number=pick.gate_number,
                )
            )
        items_by_event: dict[int, list[Win5ExportJudgementItem]] = defaultdict(list)
        for item in item_rows:
            items_by_event[item.score_event_id].append(
                Win5ExportJudgementItem(
                    id=item.id,
                    race_id=item.race_id,
                    position=item.position,
                    submission_pick_id=item.submission_pick_id,
                    matched_result_id=item.matched_result_id,
                    outcome=Win5JudgementOutcome(item.outcome),
                    season_score_delta=item.season_score_delta,
                )
            )
        event_by_submission: dict[int, Win5ExportScoreEvent] = {}
        for event in event_rows:
            if event.submission_id in event_by_submission:
                raise ValueError("Multiple WIN5 ScoreEvents reference one Submission.")
            event_by_submission[event.submission_id] = Win5ExportScoreEvent(
                id=event.id,
                season_id=event.season_id,
                round_id=event.round_id,
                persona_id=event.persona_id,
                submission_version=event.submission_version,
                tier=Win5SubmissionTier(event.tier),
                exact_count=event.exact_count,
                wrong_position_count=event.wrong_position_count,
                off_board_count=event.off_board_count,
                missing_count=event.missing_count,
                season_score_delta=event.season_score_delta,
                top1_score_delta=event.top1_score_delta,
                circle_point_reward=event.circle_point_reward,
                created_at=from_database_utc(
                    event.created_at,
                    field_name="win5_score_events.created_at",
                ),
                items=tuple(items_by_event[event.id]),
            )
        submissions = tuple(
            Win5ExportSubmission(
                id=submission.id,
                round_id=submission.round_id,
                persona_id=submission.persona_id,
                display_name=display_name,
                tier=Win5SubmissionTier(submission.tier),
                status=Win5SubmissionStatus(submission.status),
                version=submission.version,
                created_at=from_database_utc(
                    submission.created_at,
                    field_name="win5_submissions.created_at",
                ),
                updated_at=from_database_utc(
                    submission.updated_at,
                    field_name="win5_submissions.updated_at",
                ),
                picks=tuple(picks_by_submission[submission.id]),
                score_event=event_by_submission.get(submission.id),
            )
            for submission, display_name in submission_rows
        )
        scores = tuple(
            Win5ExportScore(
                persona_id=score.persona_id,
                display_name=display_name,
                season_score=score.season_score,
                top1_score=score.top1_score,
            )
            for score, display_name in score_rows
        )
        return Win5SeasonExportSource(
            season_id=season.id,
            season_name=season.name,
            season_status=Win5SeasonStatus(season.status),
            starts_at=_optional_utc(
                season.starts_at,
                field_name="win5_seasons.starts_at",
            ),
            ends_at=_optional_utc(
                season.ends_at,
                field_name="win5_seasons.ends_at",
            ),
            source_cutoff=source_cutoff,
            rounds=rounds,
            submissions=submissions,
            scores=scores,
        )


class SqlAlchemyWin5SeasonExportUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW exposing complete WIN5 Season snapshots."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._exports: SqlAlchemyWin5SeasonExportRepository | None = None

    @property
    def win5_season_exports(self) -> SqlAlchemyWin5SeasonExportRepository:
        return self._require_active_repository(self._exports)

    def _activate_repositories(self) -> None:
        self._exports = SqlAlchemyWin5SeasonExportRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._exports = None


class SqlAlchemyWin5SeasonExportUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyWin5SeasonExportUnitOfWork]
):
    """Create one fresh WIN5 Season export query UoW."""

    unit_of_work_type = SqlAlchemyWin5SeasonExportUnitOfWork
