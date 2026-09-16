"""SQLAlchemy projections for private Account/Persona status tabs."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Numeric, case, cast, func, or_, select
from sqlalchemy.orm import Session

from uma_st2.application.identity import (
    AccountGameAccountSummary,
    AccountIdentityDetails,
    AccountMatchHistoryItem,
    AccountMatchHistoryPage,
    AccountMatchSummary,
    AccountRegistrationRequestSummary,
    AccountStatusOverview,
    AccountWin5HistoryItem,
    AccountWin5HistoryPage,
    AccountWin5Summary,
)
from uma_st2.domain.identity import GameRegion, PersonaStatus, RegistrationRequestStatus
from uma_st2.domain.match import MatchSourceKind, MatchStatus
from uma_st2.domain.win5 import Win5RoundStatus, Win5RoundType, Win5SeasonStatus, Win5SubmissionStatus

from .datetime_codec import from_database_utc, to_database_utc
from .orm import (
    CirclePointORM,
    DiscordAccountORM,
    GameAccountORM,
    GameAccountRegistrationRequestORM,
    MatchEntryORM,
    MatchORM,
    PersonaORM,
    RatingORM,
    RatingTransactionORM,
    UmamusumeORM,
    UmamusumeVariantORM,
    Win5RoundORM,
    Win5ScoreEventORM,
    Win5ScoreORM,
    Win5SeasonORM,
    Win5SubmissionORM,
)
from .uow import SessionFactory, SqlAlchemyFeatureUnitOfWork, SqlAlchemyFeatureUnitOfWorkFactory
from .win5_season_marker import validate_win5_season_marker

_OFFICIAL_MATCH_STATUSES = (MatchStatus.RESULT_CONFIRMED.value, MatchStatus.SETTLED.value)
_RECENT_MATCH_STATUSES = (
    MatchStatus.RESULT_CONFIRMED.value,
    MatchStatus.SETTLED.value,
    MatchStatus.CANCELLED.value,
    MatchStatus.VOIDED.value,
)
_EXCLUDED_MATCH_STATUSES = (MatchStatus.CANCELLED.value, MatchStatus.VOIDED.value)


def _pid_hint(uma_pid: str | None) -> str | None:
    if uma_pid is None:
        return None
    normalized = uma_pid.strip()
    if not normalized:
        return None
    return f"••••{normalized[-4:]}" if len(normalized) > 4 else "••••"


def _decimal(value: object | None) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


class SqlAlchemyAccountStatusQueryRepository:
    """Materialize one Discord actor's detached Account status projections."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_overview(
        self,
        *,
        discord_user_id: str,
        guild_id: str,
        match_season_key: str,
        match_season_name: str,
        match_starts_at: datetime,
        match_ends_at: datetime,
    ) -> AccountStatusOverview:
        persona = self._find_persona(discord_user_id=discord_user_id)
        registration = self._latest_registration_request(
            discord_user_id=discord_user_id,
            guild_id=guild_id,
        )
        if persona is None:
            return AccountStatusOverview(
                persona_id=None,
                display_name=None,
                persona_status=None,
                wallet_balance=None,
                game_account_count=0,
                eligible_game_account_count=0,
                registration_request=registration,
                match=self._empty_match_summary(match_season_key, match_season_name),
                win5=None,
            )

        persona_id, display_name, status = persona
        wallet_balance = self._session.scalar(
            select(CirclePointORM.balance).where(CirclePointORM.persona_id == persona_id)
        )
        account_counts = self._session.execute(
            select(
                func.count(GameAccountORM.id),
                func.sum(case((GameAccountORM.uma_pid.is_not(None), 1), else_=0)),
            ).where(GameAccountORM.persona_id == persona_id)
        ).one()
        return AccountStatusOverview(
            persona_id=persona_id,
            display_name=display_name,
            persona_status=PersonaStatus(status),
            wallet_balance=wallet_balance,
            game_account_count=int(account_counts[0] or 0),
            eligible_game_account_count=int(account_counts[1] or 0),
            registration_request=registration,
            match=self._match_summary(
                persona_id=persona_id,
                season_key=match_season_key,
                season_name=match_season_name,
                starts_at=match_starts_at,
                ends_at=match_ends_at,
            ),
            win5=self._win5_summary(persona_id=persona_id),
        )

    def get_match_history(
        self,
        *,
        discord_user_id: str,
        match_season_key: str,
        match_season_name: str,
        match_starts_at: datetime,
        match_ends_at: datetime,
        page: int,
        offset: int,
        limit: int,
    ) -> AccountMatchHistoryPage:
        persona = self._find_persona(discord_user_id=discord_user_id)
        if persona is None:
            return AccountMatchHistoryPage(
                season_key=match_season_key,
                season_name=match_season_name,
                page=page,
                total_count=0,
                items=(),
            )
        persona_id = persona[0]
        starts_at = to_database_utc(match_starts_at, field_name="match_starts_at")
        ends_at = to_database_utc(match_ends_at, field_name="match_ends_at")
        predicates = (
            MatchEntryORM.owner_at_event_persona_id == persona_id,
            MatchORM.scheduled_at >= starts_at,
            MatchORM.scheduled_at < ends_at,
            MatchORM.status.in_(_RECENT_MATCH_STATUSES),
        )
        total_count = self._session.scalar(
            select(func.count(MatchEntryORM.id))
            .join(MatchORM, MatchORM.id == MatchEntryORM.match_id)
            .where(*predicates)
        )

        field_sizes = (
            select(
                MatchEntryORM.match_id.label("match_id"),
                func.count(MatchEntryORM.id).label("field_size"),
            )
            .group_by(MatchEntryORM.match_id)
            .subquery("account_status_field_sizes")
        )
        rating_deltas = (
            select(
                RatingTransactionORM.match_entry_id.label("match_entry_id"),
                func.sum(RatingTransactionORM.amount).label("rating_delta"),
            )
            .group_by(RatingTransactionORM.match_entry_id)
            .subquery("account_status_rating_deltas")
        )
        character_name = case(
            (
                UmamusumeVariantORM.id.is_not(None),
                func.coalesce(UmamusumeVariantORM.name_ko, UmamusumeVariantORM.name_jp),
            ),
            else_=func.coalesce(UmamusumeORM.name_ko, UmamusumeORM.name_jp),
        )
        rows = self._session.execute(
            select(
                MatchORM.id.label("match_id"),
                MatchORM.name.label("match_name"),
                MatchORM.scheduled_at,
                MatchORM.status,
                MatchORM.source_kind,
                MatchEntryORM.game_account_id,
                GameAccountORM.nickname.label("game_account_nickname"),
                character_name.label("character_name"),
                MatchEntryORM.entry_number,
                MatchEntryORM.rank,
                field_sizes.c.field_size,
                rating_deltas.c.rating_delta,
            )
            .join(MatchEntryORM, MatchEntryORM.match_id == MatchORM.id)
            .join(GameAccountORM, GameAccountORM.id == MatchEntryORM.game_account_id)
            .join(UmamusumeORM, UmamusumeORM.id == MatchEntryORM.umamusume_id)
            .outerjoin(UmamusumeVariantORM, UmamusumeVariantORM.id == MatchEntryORM.umamusume_variant_id)
            .join(field_sizes, field_sizes.c.match_id == MatchORM.id)
            .outerjoin(rating_deltas, rating_deltas.c.match_entry_id == MatchEntryORM.id)
            .where(*predicates)
            .order_by(MatchORM.scheduled_at.desc(), MatchORM.id.desc(), MatchEntryORM.entry_number)
            .offset(offset)
            .limit(limit)
        )
        return AccountMatchHistoryPage(
            season_key=match_season_key,
            season_name=match_season_name,
            page=page,
            total_count=int(total_count or 0),
            items=tuple(
                AccountMatchHistoryItem(
                    match_id=row.match_id,
                    match_name=row.match_name,
                    scheduled_at=from_database_utc(row.scheduled_at, field_name="Match scheduled_at"),
                    status=MatchStatus(row.status),
                    source_kind=MatchSourceKind(row.source_kind),
                    game_account_id=row.game_account_id,
                    game_account_nickname=row.game_account_nickname,
                    character_name=row.character_name,
                    entry_number=row.entry_number,
                    rank=row.rank,
                    field_size=row.field_size,
                    rating_delta=_decimal(row.rating_delta),
                )
                for row in rows
            ),
        )

    def get_win5_history(
        self,
        *,
        discord_user_id: str,
        page: int,
        offset: int,
        limit: int,
    ) -> AccountWin5HistoryPage:
        persona = self._find_persona(discord_user_id=discord_user_id)
        if persona is None:
            return AccountWin5HistoryPage(season=None, page=page, total_count=0, items=())
        persona_id = persona[0]
        season = self._win5_summary(persona_id=persona_id)
        if season is None:
            return AccountWin5HistoryPage(season=None, page=page, total_count=0, items=())

        latest_submissions = (
            select(
                Win5SubmissionORM.id.label("submission_id"),
                Win5SubmissionORM.round_id.label("round_id"),
                Win5SubmissionORM.status.label("submission_status"),
                Win5SubmissionORM.updated_at.label("submission_updated_at"),
                func.row_number()
                .over(
                    partition_by=Win5SubmissionORM.round_id,
                    order_by=(Win5SubmissionORM.updated_at.desc(), Win5SubmissionORM.id.desc()),
                )
                .label("row_number"),
            )
            .where(Win5SubmissionORM.persona_id == persona_id)
            .subquery("account_status_latest_submissions")
        )
        predicates = (
            Win5RoundORM.season_id == season.season_id,
            latest_submissions.c.row_number == 1,
        )
        total_count = self._session.scalar(
            select(func.count())
            .select_from(latest_submissions)
            .join(Win5RoundORM, Win5RoundORM.id == latest_submissions.c.round_id)
            .where(*predicates)
        )
        rows = self._session.execute(
            select(
                Win5RoundORM.id.label("round_id"),
                Win5RoundORM.name.label("round_name"),
                Win5RoundORM.type.label("round_type"),
                Win5RoundORM.status.label("round_status"),
                latest_submissions.c.submission_status,
                Win5ScoreEventORM.season_score_delta.label("score_delta"),
                latest_submissions.c.submission_updated_at,
            )
            .join(latest_submissions, latest_submissions.c.round_id == Win5RoundORM.id)
            .outerjoin(Win5ScoreEventORM, Win5ScoreEventORM.submission_id == latest_submissions.c.submission_id)
            .where(*predicates)
            .order_by(latest_submissions.c.submission_updated_at.desc(), Win5RoundORM.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return AccountWin5HistoryPage(
            season=season,
            page=page,
            total_count=int(total_count or 0),
            items=tuple(
                AccountWin5HistoryItem(
                    round_id=row.round_id,
                    round_name=row.round_name,
                    round_type=Win5RoundType(row.round_type),
                    round_status=Win5RoundStatus(row.round_status),
                    submission_status=Win5SubmissionStatus(row.submission_status),
                    score_delta=row.score_delta,
                    updated_at=from_database_utc(
                        row.submission_updated_at,
                        field_name="WIN5 Submission updated_at",
                    ),
                )
                for row in rows
            ),
        )

    def get_identity_details(
        self,
        *,
        discord_user_id: str,
        guild_id: str,
        page: int,
        offset: int,
        limit: int,
    ) -> AccountIdentityDetails:
        persona = self._find_persona(discord_user_id=discord_user_id)
        registration = self._latest_registration_request(
            discord_user_id=discord_user_id,
            guild_id=guild_id,
        )
        if persona is None:
            return AccountIdentityDetails(
                persona_id=None,
                display_name=None,
                persona_status=None,
                page=page,
                total_count=0,
                accounts=(),
                registration_request=registration,
            )

        persona_id, display_name, status = persona
        total_count = self._session.scalar(
            select(func.count(GameAccountORM.id)).where(GameAccountORM.persona_id == persona_id)
        )
        ranked = select(
            RatingORM.game_account_id.label("game_account_id"),
            RatingORM.rating.label("current_rating"),
            func.rank().over(order_by=RatingORM.rating.desc()).label("competition_rank"),
        ).subquery("account_status_ranked_ratings")
        rows = self._session.execute(
            select(
                GameAccountORM.id,
                GameAccountORM.game_region,
                GameAccountORM.uma_pid,
                GameAccountORM.nickname,
                GameAccountORM.affiliation,
                ranked.c.current_rating,
                ranked.c.competition_rank,
            )
            .outerjoin(ranked, ranked.c.game_account_id == GameAccountORM.id)
            .where(GameAccountORM.persona_id == persona_id)
            .order_by(GameAccountORM.id)
            .offset(offset)
            .limit(limit)
        )
        return AccountIdentityDetails(
            persona_id=persona_id,
            display_name=display_name,
            persona_status=PersonaStatus(status),
            page=page,
            total_count=int(total_count or 0),
            accounts=tuple(
                AccountGameAccountSummary(
                    game_account_id=row.id,
                    game_region=GameRegion(row.game_region),
                    pid_hint=_pid_hint(row.uma_pid),
                    nickname=row.nickname,
                    affiliation=row.affiliation,
                    current_rating=_decimal(row.current_rating),
                    competition_rank=row.competition_rank,
                )
                for row in rows
            ),
            registration_request=registration,
        )

    def _find_persona(self, *, discord_user_id: str) -> tuple[str, str, str] | None:
        row = self._session.execute(
            select(PersonaORM.id, PersonaORM.display_name, PersonaORM.status)
            .join(DiscordAccountORM, DiscordAccountORM.persona_id == PersonaORM.id)
            .where(DiscordAccountORM.discord_user_id == discord_user_id)
        ).one_or_none()
        return None if row is None else (row.id, row.display_name, row.status)

    def _latest_registration_request(
        self,
        *,
        discord_user_id: str,
        guild_id: str,
    ) -> AccountRegistrationRequestSummary | None:
        row = self._session.execute(
            select(GameAccountRegistrationRequestORM)
            .where(
                GameAccountRegistrationRequestORM.guild_id == guild_id,
                GameAccountRegistrationRequestORM.requester_discord_user_id == discord_user_id,
            )
            .order_by(
                GameAccountRegistrationRequestORM.created_at.desc(),
                GameAccountRegistrationRequestORM.id.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        pid_hint = _pid_hint(row.uma_pid)
        if pid_hint is None:
            raise ValueError("Registration request PID is empty.")
        return AccountRegistrationRequestSummary(
            request_id=row.id,
            game_region=GameRegion(row.game_region),
            pid_hint=pid_hint,
            nickname=row.nickname,
            status=RegistrationRequestStatus(row.status),
            reason=row.reason,
            created_at=from_database_utc(row.created_at, field_name="registration created_at"),
            resolved_at=(
                None
                if row.resolved_at is None
                else from_database_utc(row.resolved_at, field_name="registration resolved_at")
            ),
        )

    def _match_summary(
        self,
        *,
        persona_id: str,
        season_key: str,
        season_name: str,
        starts_at: datetime,
        ends_at: datetime,
    ) -> AccountMatchSummary:
        starts_at_db = to_database_utc(starts_at, field_name="match_starts_at")
        ends_at_db = to_database_utc(ends_at, field_name="match_ends_at")
        official = self._session.execute(
            select(
                func.count(func.distinct(MatchORM.id)).label("match_count"),
                func.count(MatchEntryORM.id).label("entry_count"),
                func.sum(case((MatchEntryORM.rank == 1, 1), else_=0)).label("win_count"),
                func.sum(case((MatchEntryORM.rank <= 3, 1), else_=0)).label("top3_count"),
                func.avg(cast(MatchEntryORM.rank, Numeric(30, 18))).label("average_rank"),
            )
            .join(MatchORM, MatchORM.id == MatchEntryORM.match_id)
            .where(
                MatchEntryORM.owner_at_event_persona_id == persona_id,
                MatchEntryORM.rank.is_not(None),
                MatchORM.status.in_(_OFFICIAL_MATCH_STATUSES),
                MatchORM.scheduled_at >= starts_at_db,
                MatchORM.scheduled_at < ends_at_db,
            )
        ).one()
        excluded = self._session.scalar(
            select(func.count(func.distinct(MatchORM.id)))
            .join(MatchEntryORM, MatchEntryORM.match_id == MatchORM.id)
            .where(
                MatchEntryORM.owner_at_event_persona_id == persona_id,
                MatchORM.status.in_(_EXCLUDED_MATCH_STATUSES),
                MatchORM.scheduled_at >= starts_at_db,
                MatchORM.scheduled_at < ends_at_db,
            )
        )
        return AccountMatchSummary(
            season_key=season_key,
            season_name=season_name,
            participated_match_count=int(official.match_count or 0),
            entry_count=int(official.entry_count or 0),
            win_count=int(official.win_count or 0),
            top3_count=int(official.top3_count or 0),
            average_rank=_decimal(official.average_rank),
            excluded_terminal_match_count=int(excluded or 0),
        )

    @staticmethod
    def _empty_match_summary(season_key: str, season_name: str) -> AccountMatchSummary:
        return AccountMatchSummary(
            season_key=season_key,
            season_name=season_name,
            participated_match_count=0,
            entry_count=0,
            win_count=0,
            top3_count=0,
            average_rank=None,
            excluded_terminal_match_count=0,
        )

    def _win5_summary(self, *, persona_id: str) -> AccountWin5Summary | None:
        active_candidates = (
            self._session.execute(
                select(Win5SeasonORM)
                .where(
                    or_(
                        Win5SeasonORM.status == Win5SeasonStatus.ACTIVE.value,
                        Win5SeasonORM.active_marker.is_(True),
                    )
                )
                .order_by(Win5SeasonORM.id.desc())
            )
            .scalars()
            .all()
        )
        if len(active_candidates) > 1:
            raise ValueError("Multiple active WIN5 Season candidates exist.")
        season = active_candidates[0] if active_candidates else None
        if season is not None:
            validate_win5_season_marker(
                status=season.status,
                active_marker=season.active_marker,
            )
        if season is None:
            season = self._session.execute(
                select(Win5SeasonORM)
                .where(Win5SeasonORM.status == Win5SeasonStatus.CLOSED.value)
                .order_by(Win5SeasonORM.updated_at.desc(), Win5SeasonORM.id.desc())
                .limit(1)
            ).scalar_one_or_none()
        if season is None:
            return None
        validate_win5_season_marker(
            status=season.status,
            active_marker=season.active_marker,
        )

        ranked = (
            select(
                Win5ScoreORM.persona_id.label("persona_id"),
                Win5ScoreORM.season_score.label("season_score"),
                func.rank().over(order_by=Win5ScoreORM.season_score.desc()).label("competition_rank"),
            )
            .where(Win5ScoreORM.season_id == season.id)
            .subquery("account_status_ranked_win5_scores")
        )
        score = self._session.execute(
            select(ranked.c.season_score, ranked.c.competition_rank).where(ranked.c.persona_id == persona_id)
        ).one_or_none()
        submitted_round_count = self._session.scalar(
            select(func.count(func.distinct(Win5SubmissionORM.round_id)))
            .join(Win5RoundORM, Win5RoundORM.id == Win5SubmissionORM.round_id)
            .where(
                Win5SubmissionORM.persona_id == persona_id,
                Win5RoundORM.season_id == season.id,
            )
        )
        scored_round_count = self._session.scalar(
            select(func.count(func.distinct(Win5ScoreEventORM.round_id))).where(
                Win5ScoreEventORM.persona_id == persona_id,
                Win5ScoreEventORM.season_id == season.id,
            )
        )
        return AccountWin5Summary(
            season_id=season.id,
            season_name=season.name,
            season_status=Win5SeasonStatus(season.status),
            season_score=0 if score is None else score.season_score,
            competition_rank=None if score is None else score.competition_rank,
            submitted_round_count=int(submitted_round_count or 0),
            scored_round_count=int(scored_round_count or 0),
        )


class SqlAlchemyAccountStatusQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW for private Account status."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._queries: SqlAlchemyAccountStatusQueryRepository | None = None

    @property
    def account_status_queries(self) -> SqlAlchemyAccountStatusQueryRepository:
        return self._require_active_repository(self._queries)

    def _activate_repositories(self) -> None:
        self._queries = SqlAlchemyAccountStatusQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._queries = None


class SqlAlchemyAccountStatusQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyAccountStatusQueryUnitOfWork]
):
    """Create one fresh Account status query UoW per tab read."""

    unit_of_work_type = SqlAlchemyAccountStatusQueryUnitOfWork
