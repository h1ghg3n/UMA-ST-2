"""SQLAlchemy implementation of member-facing WIN5 queries."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from uma_st2.application.win5.member_identity import Win5MemberPersona
from uma_st2.application.win5.member_queries import (
    Win5ActiveSeasonInfo,
    Win5CancellableSubmission,
    Win5NormalSubmissionEditorSource,
    Win5NormalSubmissionRoundChoice,
    Win5NormalSubmissionSource,
    Win5OpenRoundCard,
    Win5RaceCard,
    Win5RaceEntryOption,
    Win5SeasonChoice,
    Win5SpecialSubmissionEditorSource,
    Win5SpecialSubmissionRoundChoice,
    Win5SpecialSubmissionSource,
    Win5StandingRanking,
    Win5StandingScore,
    Win5StandingsSource,
)
from uma_st2.application.win5.round_lifecycle import MAX_OPEN_WIN5_ROUNDS_PER_ACTIVE_SEASON
from uma_st2.domain.win5 import (
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SubmissionPick,
    Win5SubmissionStatus,
    Win5SubmissionTier,
)

from .datetime_codec import from_database_utc
from .orm import (
    PersonaORM,
    Win5RaceEntryORM,
    Win5RaceORM,
    Win5RoundORM,
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
from .win5_member_identity import find_win5_member_persona


class SqlAlchemyWin5MemberQueryRepository:
    """Build bounded immutable WIN5 projections from one active Session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def _optional_utc(value: datetime | None, *, field_name: str) -> datetime | None:
        if value is None:
            return None
        return from_database_utc(value, field_name=field_name)

    def find_member_persona(self, *, discord_user_id: str) -> Win5MemberPersona | None:
        return find_win5_member_persona(self._session, discord_user_id=discord_user_id)

    def has_active_open_round_overflow(self) -> bool:
        overflow_season = (
            select(Win5RoundORM.season_id)
            .join(Win5SeasonORM, Win5SeasonORM.id == Win5RoundORM.season_id)
            .where(
                Win5SeasonORM.status == Win5SeasonStatus.ACTIVE.value,
                Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
                Win5RoundORM.status == Win5RoundStatus.OPEN.value,
            )
            .group_by(Win5RoundORM.season_id)
            .having(func.count(Win5RoundORM.id) > MAX_OPEN_WIN5_ROUNDS_PER_ACTIVE_SEASON)
            .limit(1)
        )
        return self._session.scalar(overflow_season) is not None

    @staticmethod
    def _cancellable_submission(
        *,
        season: Win5SeasonORM,
        round_: Win5RoundORM,
        submission: Win5SubmissionORM,
        pick_count: int,
    ) -> Win5CancellableSubmission:
        return Win5CancellableSubmission(
            season_id=season.id,
            season_name=season.name,
            round_id=round_.id,
            round_name=round_.name,
            round_type=Win5RoundType(round_.type),
            submission_id=submission.id,
            tier=Win5SubmissionTier(submission.tier),
            version=submission.version,
            pick_count=pick_count,
        )

    @staticmethod
    def _cancellable_submission_statement(*, persona_id: str):
        pick_count = (
            select(func.count(Win5SubmissionPickORM.id))
            .where(Win5SubmissionPickORM.submission_id == Win5SubmissionORM.id)
            .correlate(Win5SubmissionORM)
            .scalar_subquery()
        )
        return (
            select(
                Win5SeasonORM,
                Win5RoundORM,
                Win5SubmissionORM,
                pick_count.label("pick_count"),
            )
            .join(Win5RoundORM, Win5RoundORM.season_id == Win5SeasonORM.id)
            .join(Win5SubmissionORM, Win5SubmissionORM.round_id == Win5RoundORM.id)
            .where(
                Win5SeasonORM.status == Win5SeasonStatus.ACTIVE.value,
                Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
                Win5RoundORM.status == Win5RoundStatus.OPEN.value,
                Win5SubmissionORM.persona_id == persona_id,
                Win5SubmissionORM.status == Win5SubmissionStatus.ACCEPTED.value,
                Win5SubmissionORM.active_marker.is_(True),
            )
        )

    def get_active_season_info(self, *, persona_id: str) -> Win5ActiveSeasonInfo | None:
        season = self._session.execute(
            select(Win5SeasonORM).where(Win5SeasonORM.status == Win5SeasonStatus.ACTIVE.value)
        ).scalar_one_or_none()
        if season is None:
            return None

        total_round_count = self._session.scalar(
            select(func.count(Win5RoundORM.id)).where(Win5RoundORM.season_id == season.id)
        )
        open_round_count = self._session.scalar(
            select(func.count(Win5RoundORM.id)).where(
                Win5RoundORM.season_id == season.id,
                Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
                Win5RoundORM.status == Win5RoundStatus.OPEN.value,
            )
        )
        score = self._session.execute(
            select(Win5ScoreORM.season_score, Win5ScoreORM.top1_score).where(
                Win5ScoreORM.season_id == season.id,
                Win5ScoreORM.persona_id == persona_id,
            )
        ).one_or_none()

        return Win5ActiveSeasonInfo(
            season_id=season.id,
            season_name=season.name,
            season_status=Win5SeasonStatus(season.status),
            starts_at=self._optional_utc(
                season.starts_at,
                field_name="win5_seasons.starts_at",
            ),
            ends_at=self._optional_utc(
                season.ends_at,
                field_name="win5_seasons.ends_at",
            ),
            total_round_count=total_round_count or 0,
            open_round_count=open_round_count or 0,
            season_score=score.season_score if score is not None else 0,
            top1_score=score.top1_score if score is not None else 0,
        )

    def list_open_rounds(self, *, limit: int) -> tuple[Win5OpenRoundCard, ...]:
        season_round_rows = self._session.execute(
            select(Win5SeasonORM, Win5RoundORM)
            .join(Win5RoundORM, Win5RoundORM.season_id == Win5SeasonORM.id)
            .where(
                Win5SeasonORM.status == Win5SeasonStatus.ACTIVE.value,
                Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
                Win5RoundORM.status == Win5RoundStatus.OPEN.value,
            )
            .order_by(Win5SeasonORM.id, Win5RoundORM.created_at, Win5RoundORM.id)
            .limit(limit)
        ).all()
        if not season_round_rows:
            return ()

        round_ids = tuple(round_.id for _, round_ in season_round_rows)
        race_rows = tuple(
            self._session.scalars(
                select(Win5RaceORM)
                .where(Win5RaceORM.round_id.in_(round_ids))
                .order_by(Win5RaceORM.round_id, Win5RaceORM.id)
            )
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

        entries_by_race_id: dict[int, list[Win5RaceEntryOption]] = defaultdict(list)
        for entry in entry_rows:
            entries_by_race_id[entry.race_id].append(
                Win5RaceEntryOption(
                    id=entry.id,
                    gate_number=entry.gate_number,
                    name=entry.name,
                )
            )

        races_by_round_id: dict[int, list[Win5RaceCard]] = defaultdict(list)
        for race in race_rows:
            races_by_round_id[race.round_id].append(
                Win5RaceCard(
                    id=race.id,
                    name=race.name,
                    scheduled_at=self._optional_utc(
                        race.scheduled_at,
                        field_name="win5_races.scheduled_at",
                    ),
                    entries=tuple(entries_by_race_id[race.id]),
                )
            )

        return tuple(
            Win5OpenRoundCard(
                id=round_.id,
                season_id=season.id,
                season_name=season.name,
                round_type=Win5RoundType(round_.type),
                name=round_.name,
                opens_at=self._optional_utc(
                    round_.opens_at,
                    field_name="win5_rounds.opens_at",
                ),
                closes_at=self._optional_utc(
                    round_.closes_at,
                    field_name="win5_rounds.closes_at",
                ),
                races=tuple(races_by_round_id[round_.id]),
            )
            for season, round_ in season_round_rows
        )

    def search_normal_submission_rounds(
        self,
        *,
        query: str,
        limit: int,
    ) -> tuple[Win5NormalSubmissionRoundChoice, ...]:
        statement = (
            select(Win5SeasonORM, Win5RoundORM)
            .join(Win5RoundORM, Win5RoundORM.season_id == Win5SeasonORM.id)
            .where(
                Win5SeasonORM.status == Win5SeasonStatus.ACTIVE.value,
                Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
                Win5RoundORM.type == Win5RoundType.NORMAL.value,
                Win5RoundORM.status == Win5RoundStatus.OPEN.value,
            )
        )
        if query:
            race_round_ids = select(Win5RaceORM.round_id).where(Win5RaceORM.name.contains(query, autoescape=True))
            statement = statement.where(
                or_(
                    Win5SeasonORM.name.contains(query, autoescape=True),
                    Win5RoundORM.name.contains(query, autoescape=True),
                    Win5RoundORM.id.in_(race_round_ids),
                )
            )
        season_round_rows = self._session.execute(
            statement.order_by(
                Win5SeasonORM.id,
                Win5RoundORM.created_at,
                Win5RoundORM.id,
            ).limit(limit)
        ).all()
        if not season_round_rows:
            return ()

        round_ids = tuple(round_.id for _, round_ in season_round_rows)
        races_by_round_id: dict[int, list[Win5RaceORM]] = defaultdict(list)
        for race in self._session.scalars(
            select(Win5RaceORM)
            .where(Win5RaceORM.round_id.in_(round_ids))
            .order_by(Win5RaceORM.round_id, Win5RaceORM.id)
        ):
            races_by_round_id[race.round_id].append(race)

        choices: list[Win5NormalSubmissionRoundChoice] = []
        for season, round_ in season_round_rows:
            races = races_by_round_id[round_.id]
            if len(races) != 1:
                continue
            race = races[0]
            choices.append(
                Win5NormalSubmissionRoundChoice(
                    season_id=season.id,
                    season_name=season.name,
                    round_id=round_.id,
                    round_name=round_.name,
                    race_id=race.id,
                    race_name=race.name,
                )
            )
        return tuple(choices)

    def search_special_submission_rounds(
        self,
        *,
        query: str,
        limit: int,
    ) -> tuple[Win5SpecialSubmissionRoundChoice, ...]:
        statement = (
            select(Win5SeasonORM, Win5RoundORM)
            .join(Win5RoundORM, Win5RoundORM.season_id == Win5SeasonORM.id)
            .where(
                Win5SeasonORM.status == Win5SeasonStatus.ACTIVE.value,
                Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
                Win5RoundORM.type == Win5RoundType.SPECIAL.value,
                Win5RoundORM.status == Win5RoundStatus.OPEN.value,
            )
        )
        if query:
            race_round_ids = select(Win5RaceORM.round_id).where(Win5RaceORM.name.contains(query, autoescape=True))
            statement = statement.where(
                or_(
                    Win5SeasonORM.name.contains(query, autoescape=True),
                    Win5RoundORM.name.contains(query, autoescape=True),
                    Win5RoundORM.id.in_(race_round_ids),
                )
            )
        season_round_rows = self._session.execute(
            statement.order_by(
                Win5SeasonORM.id,
                Win5RoundORM.created_at,
                Win5RoundORM.id,
            ).limit(limit)
        ).all()
        if not season_round_rows:
            return ()

        round_ids = tuple(round_.id for _, round_ in season_round_rows)
        race_counts = dict(
            self._session.execute(
                select(Win5RaceORM.round_id, func.count(Win5RaceORM.id))
                .where(Win5RaceORM.round_id.in_(round_ids))
                .group_by(Win5RaceORM.round_id)
            ).all()
        )
        return tuple(
            Win5SpecialSubmissionRoundChoice(
                season_id=season.id,
                season_name=season.name,
                round_id=round_.id,
                round_name=round_.name,
                race_count=race_counts[round_.id],
            )
            for season, round_ in season_round_rows
            if race_counts.get(round_.id, 0) > 0
        )

    def get_normal_submission_editor_source(
        self,
        *,
        round_id: int,
        persona_id: str,
    ) -> Win5NormalSubmissionEditorSource | None:
        season_round = self._session.execute(
            select(Win5SeasonORM, Win5RoundORM)
            .join(Win5RoundORM, Win5RoundORM.season_id == Win5SeasonORM.id)
            .where(
                Win5RoundORM.id == round_id,
                Win5SeasonORM.status == Win5SeasonStatus.ACTIVE.value,
                Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
                Win5RoundORM.type == Win5RoundType.NORMAL.value,
                Win5RoundORM.status == Win5RoundStatus.OPEN.value,
            )
        ).one_or_none()
        if season_round is None:
            return None
        season, round_ = season_round

        races = tuple(
            self._session.scalars(select(Win5RaceORM).where(Win5RaceORM.round_id == round_.id).order_by(Win5RaceORM.id))
        )
        race_ids = tuple(race.id for race in races)
        entries_by_race_id: dict[int, list[Win5RaceEntryOption]] = defaultdict(list)
        if race_ids:
            for entry in self._session.scalars(
                select(Win5RaceEntryORM)
                .where(Win5RaceEntryORM.race_id.in_(race_ids))
                .order_by(
                    Win5RaceEntryORM.race_id,
                    Win5RaceEntryORM.gate_number,
                    Win5RaceEntryORM.id,
                )
            ):
                entries_by_race_id[entry.race_id].append(
                    Win5RaceEntryOption(
                        id=entry.id,
                        gate_number=entry.gate_number,
                        name=entry.name,
                    )
                )

        submission = self._session.execute(
            select(Win5SubmissionORM).where(
                Win5SubmissionORM.round_id == round_.id,
                Win5SubmissionORM.persona_id == persona_id,
                Win5SubmissionORM.status == Win5SubmissionStatus.ACCEPTED.value,
                Win5SubmissionORM.active_marker.is_(True),
            )
        ).scalar_one_or_none()
        submission_source: Win5NormalSubmissionSource | None = None
        if submission is not None:
            picks = tuple(
                Win5SubmissionPick(
                    id=pick.id,
                    submission_id=pick.submission_id,
                    race_id=pick.race_id,
                    race_entry_id=pick.race_entry_id,
                    position=pick.position,
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
            submission_source = Win5NormalSubmissionSource(
                id=submission.id,
                round_id=submission.round_id,
                persona_id=submission.persona_id,
                tier=Win5SubmissionTier(submission.tier),
                status=Win5SubmissionStatus(submission.status),
                active_marker=submission.active_marker,
                version=submission.version,
                picks=picks,
            )

        return Win5NormalSubmissionEditorSource(
            season_id=season.id,
            season_name=season.name,
            season_status=Win5SeasonStatus(season.status),
            round_id=round_.id,
            round_name=round_.name,
            round_type=Win5RoundType(round_.type),
            round_status=Win5RoundStatus(round_.status),
            races=tuple(
                Win5RaceCard(
                    id=race.id,
                    name=race.name,
                    scheduled_at=self._optional_utc(
                        race.scheduled_at,
                        field_name="win5_races.scheduled_at",
                    ),
                    entries=tuple(entries_by_race_id[race.id]),
                )
                for race in races
            ),
            submission=submission_source,
        )

    def get_special_submission_editor_source(
        self,
        *,
        round_id: int,
        persona_id: str,
    ) -> Win5SpecialSubmissionEditorSource | None:
        season_round = self._session.execute(
            select(Win5SeasonORM, Win5RoundORM)
            .join(Win5RoundORM, Win5RoundORM.season_id == Win5SeasonORM.id)
            .where(
                Win5RoundORM.id == round_id,
                Win5SeasonORM.status == Win5SeasonStatus.ACTIVE.value,
                Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
                Win5RoundORM.type == Win5RoundType.SPECIAL.value,
                Win5RoundORM.status == Win5RoundStatus.OPEN.value,
            )
        ).one_or_none()
        if season_round is None:
            return None
        season, round_ = season_round

        races = tuple(
            self._session.scalars(select(Win5RaceORM).where(Win5RaceORM.round_id == round_.id).order_by(Win5RaceORM.id))
        )
        race_ids = tuple(race.id for race in races)
        entries_by_race_id: dict[int, list[Win5RaceEntryOption]] = defaultdict(list)
        if race_ids:
            for entry in self._session.scalars(
                select(Win5RaceEntryORM)
                .where(Win5RaceEntryORM.race_id.in_(race_ids))
                .order_by(
                    Win5RaceEntryORM.race_id,
                    Win5RaceEntryORM.gate_number,
                    Win5RaceEntryORM.id,
                )
            ):
                entries_by_race_id[entry.race_id].append(
                    Win5RaceEntryOption(
                        id=entry.id,
                        gate_number=entry.gate_number,
                        name=entry.name,
                    )
                )

        submission = self._session.execute(
            select(Win5SubmissionORM).where(
                Win5SubmissionORM.round_id == round_.id,
                Win5SubmissionORM.persona_id == persona_id,
                Win5SubmissionORM.status == Win5SubmissionStatus.ACCEPTED.value,
                Win5SubmissionORM.active_marker.is_(True),
            )
        ).scalar_one_or_none()
        submission_source: Win5SpecialSubmissionSource | None = None
        if submission is not None:
            picks = tuple(
                Win5SubmissionPick(
                    id=pick.id,
                    submission_id=pick.submission_id,
                    race_id=pick.race_id,
                    race_entry_id=pick.race_entry_id,
                    position=pick.position,
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
            submission_source = Win5SpecialSubmissionSource(
                id=submission.id,
                round_id=submission.round_id,
                persona_id=submission.persona_id,
                tier=Win5SubmissionTier(submission.tier),
                status=Win5SubmissionStatus(submission.status),
                active_marker=submission.active_marker,
                version=submission.version,
                picks=picks,
            )

        return Win5SpecialSubmissionEditorSource(
            season_id=season.id,
            season_name=season.name,
            season_status=Win5SeasonStatus(season.status),
            round_id=round_.id,
            round_name=round_.name,
            round_type=Win5RoundType(round_.type),
            round_status=Win5RoundStatus(round_.status),
            races=tuple(
                Win5RaceCard(
                    id=race.id,
                    name=race.name,
                    scheduled_at=self._optional_utc(
                        race.scheduled_at,
                        field_name="win5_races.scheduled_at",
                    ),
                    entries=tuple(entries_by_race_id[race.id]),
                )
                for race in races
            ),
            submission=submission_source,
        )

    def search_cancellable_submissions(
        self,
        *,
        persona_id: str,
        query: str,
        limit: int,
    ) -> tuple[Win5CancellableSubmission, ...]:
        statement = self._cancellable_submission_statement(persona_id=persona_id)
        if query:
            conditions = [
                Win5SeasonORM.name.contains(query, autoescape=True),
                Win5RoundORM.name.contains(query, autoescape=True),
                Win5RoundORM.type.contains(query, autoescape=True),
                Win5SubmissionORM.tier.contains(query, autoescape=True),
            ]
            if query.isdecimal() and len(query) <= 19:
                numeric_id = int(query)
                conditions.extend(
                    (
                        Win5SubmissionORM.id == numeric_id,
                        Win5RoundORM.id == numeric_id,
                    )
                )
            statement = statement.where(or_(*conditions))

        rows = self._session.execute(statement.order_by(Win5SubmissionORM.id.desc()).limit(limit)).all()
        return tuple(
            self._cancellable_submission(
                season=season,
                round_=round_,
                submission=submission,
                pick_count=pick_count,
            )
            for season, round_, submission, pick_count in rows
        )

    def get_cancellable_submission(
        self,
        *,
        submission_id: int,
        persona_id: str,
    ) -> Win5CancellableSubmission | None:
        row = self._session.execute(
            self._cancellable_submission_statement(persona_id=persona_id).where(Win5SubmissionORM.id == submission_id)
        ).one_or_none()
        if row is None:
            return None
        season, round_, submission, pick_count = row
        return self._cancellable_submission(
            season=season,
            round_=round_,
            submission=submission,
            pick_count=pick_count,
        )

    def search_standings_seasons(self, *, query: str, limit: int) -> tuple[Win5SeasonChoice, ...]:
        available_statuses = (
            Win5SeasonStatus.ACTIVE.value,
            Win5SeasonStatus.CLOSED.value,
        )
        statement = select(Win5SeasonORM).where(Win5SeasonORM.status.in_(available_statuses))
        if query:
            statement = statement.where(Win5SeasonORM.name.contains(query, autoescape=True))
        seasons = self._session.scalars(
            statement.order_by(
                case((Win5SeasonORM.status == Win5SeasonStatus.ACTIVE.value, 0), else_=1),
                Win5SeasonORM.id.desc(),
            ).limit(limit)
        )
        return tuple(
            Win5SeasonChoice(
                id=season.id,
                name=season.name,
                status=Win5SeasonStatus(season.status),
            )
            for season in seasons
        )

    def get_standings(
        self,
        *,
        season_id: int,
        ranking: Win5StandingRanking,
        limit: int,
    ) -> Win5StandingsSource | None:
        season = self._session.execute(
            select(Win5SeasonORM).where(
                Win5SeasonORM.id == season_id,
                Win5SeasonORM.status.in_(
                    (
                        Win5SeasonStatus.ACTIVE.value,
                        Win5SeasonStatus.CLOSED.value,
                    )
                ),
            )
        ).scalar_one_or_none()
        if season is None:
            return None

        score_column = Win5ScoreORM.season_score if ranking == "season" else Win5ScoreORM.top1_score
        rows = self._session.execute(
            select(
                Win5ScoreORM.persona_id,
                PersonaORM.display_name,
                score_column.label("score"),
            )
            .join(PersonaORM, PersonaORM.id == Win5ScoreORM.persona_id)
            .where(Win5ScoreORM.season_id == season_id)
            .order_by(score_column.desc(), Win5ScoreORM.persona_id)
            .limit(limit)
        ).all()

        return Win5StandingsSource(
            season_id=season.id,
            season_name=season.name,
            season_status=Win5SeasonStatus(season.status),
            ranking=ranking,
            scores=tuple(
                Win5StandingScore(
                    persona_id=row.persona_id,
                    display_name=row.display_name,
                    score=row.score,
                )
                for row in rows
            ),
        )


class SqlAlchemyWin5MemberQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete feature UoW exposing its Session only through a repository."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._win5_member_queries: SqlAlchemyWin5MemberQueryRepository | None = None

    @property
    def win5_member_queries(self) -> SqlAlchemyWin5MemberQueryRepository:
        return self._require_active_repository(self._win5_member_queries)

    def _activate_repositories(self) -> None:
        self._win5_member_queries = SqlAlchemyWin5MemberQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._win5_member_queries = None


class SqlAlchemyWin5MemberQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyWin5MemberQueryUnitOfWork]
):
    """Create one feature query UoW per application operation."""

    unit_of_work_type = SqlAlchemyWin5MemberQueryUnitOfWork
