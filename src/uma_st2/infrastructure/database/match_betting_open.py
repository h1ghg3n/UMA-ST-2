"""SQLAlchemy implementation of native Match betting-open."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session, aliased

from uma_st2.application.match.betting_open import (
    MatchBettingOpenRatingRuleCoverage,
    MatchBettingOpenTarget,
    OpenedMatchBetting,
    OpenMatchBetting,
    StoredMatchBettingOpenOperation,
    StoredMatchOpeningPublication,
)
from uma_st2.application.publication import PublicationIntent
from uma_st2.application.publication.match import (
    MatchOpeningCondition,
    MatchOpeningCourse,
    MatchOpeningEntry,
    MatchPublicationDestination,
)
from uma_st2.application.publication.match_odds import MatchOddsRefreshMode
from uma_st2.domain.match import (
    MatchDirection,
    MatchGrade,
    MatchSeason,
    MatchSourceKind,
    MatchStatus,
    MatchSurface,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
    StadiumCourseLayout,
)

from .datetime_codec import from_database_utc, to_database_utc
from .orm import (
    BetORM,
    BotGuildSettingORM,
    DiscordPublicationORM,
    GameAccountORM,
    MatchConditionORM,
    MatchEntryORM,
    MatchOperationORM,
    MatchORM,
    OperationORM,
    RatingRuleORM,
    RatingRuleVersionORM,
    StadiumCourseORM,
    StadiumORM,
    UmamusumeORM,
    UmamusumeVariantORM,
)
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


def load_match_betting_open_rating_rule_coverages(
    session: Session,
    *,
    targets: tuple[tuple[MatchGrade, int], ...],
    lock: bool,
) -> dict[tuple[MatchGrade, int], MatchBettingOpenRatingRuleCoverage]:
    """Load current-version coverage once for a bounded set of opening targets."""

    canonical_targets = tuple(dict.fromkeys((MatchGrade(grade), field_size) for grade, field_size in targets))
    no_version = MatchBettingOpenRatingRuleCoverage(
        current_version_available=False,
        current_version_complete=False,
    )
    coverages = dict.fromkeys(canonical_targets, no_version)
    if not any(grade is not MatchGrade.OP for grade, _ in canonical_targets):
        return coverages

    version_statement = (
        select(RatingRuleVersionORM)
        .order_by(
            RatingRuleVersionORM.version_number.desc(),
            RatingRuleVersionORM.id.desc(),
        )
        .limit(1)
    )
    if lock:
        version_statement = version_statement.with_for_update()
    version = session.scalar(version_statement)
    if version is None:
        return coverages

    rows = tuple(
        session.execute(
            select(
                RatingRuleORM.grade,
                RatingRuleORM.participant_count,
                RatingRuleORM.converted_rank,
            )
            .where(RatingRuleORM.rating_rule_version_id == version.id)
            .order_by(
                RatingRuleORM.grade,
                RatingRuleORM.participant_count,
                RatingRuleORM.converted_rank,
                RatingRuleORM.id,
            )
        )
    )
    version_complete = len(rows) == version.rule_count
    ranks_by_target: dict[tuple[MatchGrade, int], list[int]] = {}
    for row in rows:
        try:
            key = MatchGrade(row.grade), row.participant_count
        except ValueError:
            continue
        ranks_by_target.setdefault(key, []).append(row.converted_rank)

    for target in canonical_targets:
        grade, _ = target
        if grade is MatchGrade.OP:
            continue
        coverages[target] = MatchBettingOpenRatingRuleCoverage(
            current_version_available=True,
            current_version_complete=version_complete,
            covered_converted_ranks=tuple(ranks_by_target.get(target, ())),
        )
    return coverages


def load_match_betting_open_target(
    session: Session,
    *,
    match_id: int,
    guild_id: str,
    lock: bool,
) -> MatchBettingOpenTarget | None:
    """Load one complete provider-independent target with optional row locks."""

    identity_statement = (
        select(MatchORM, StadiumCourseORM, StadiumORM)
        .join(StadiumCourseORM, StadiumCourseORM.id == MatchORM.stadium_course_id)
        .join(StadiumORM, StadiumORM.id == StadiumCourseORM.stadium_id)
        .where(MatchORM.id == match_id)
    )
    if lock:
        identity_statement = identity_statement.with_for_update()
    identity = session.execute(identity_statement).one_or_none()
    if identity is None:
        return None
    match, course, stadium = identity

    condition_statement = select(MatchConditionORM).where(MatchConditionORM.match_id == match_id)
    if lock:
        condition_statement = condition_statement.with_for_update()
    condition_row = session.scalar(condition_statement)

    variant = aliased(UmamusumeVariantORM)
    entries_statement = (
        select(MatchEntryORM, GameAccountORM, UmamusumeORM, variant)
        .join(GameAccountORM, GameAccountORM.id == MatchEntryORM.game_account_id)
        .join(UmamusumeORM, UmamusumeORM.id == MatchEntryORM.umamusume_id)
        .outerjoin(variant, variant.id == MatchEntryORM.umamusume_variant_id)
        .where(MatchEntryORM.match_id == match_id)
        .order_by(MatchEntryORM.entry_number, MatchEntryORM.id)
    )
    if lock:
        entries_statement = entries_statement.with_for_update()
    entry_rows = session.execute(entries_statement)

    settings_statement = select(BotGuildSettingORM).where(BotGuildSettingORM.guild_id == guild_id)
    if lock:
        settings_statement = settings_statement.with_for_update()
    setting = session.scalar(settings_statement)
    destination = MatchPublicationDestination(
        guild_id=guild_id,
        announcements_enabled=True if setting is None else setting.match_announcements_enabled,
        target_channel_id=None if setting is None else setting.match_announcement_channel_id,
    )

    grade = MatchGrade(match.grade)
    entries = tuple(
        MatchOpeningEntry(
            entry_id=entry.id,
            entry_number=entry.entry_number,
            game_account_name=account.nickname,
            horse_name=(variant_row.name_ko or variant_row.name_jp)
            if variant_row is not None
            else (base.name_ko or base.name_jp),
            affiliation=entry.affiliation_at_event,
        )
        for entry, account, base, variant_row in entry_rows
    )
    rating_rule_coverage = load_match_betting_open_rating_rule_coverages(
        session,
        targets=((grade, len(entries)),),
        lock=lock,
    )[(grade, len(entries))]

    return MatchBettingOpenTarget(
        match_id=match.id,
        match_name=match.name,
        description=match.description,
        source_kind=MatchSourceKind(match.source_kind),
        status=MatchStatus(match.status),
        grade=grade,
        scheduled_at=from_database_utc(match.scheduled_at, field_name="matches.scheduled_at"),
        course=MatchOpeningCourse(
            course_id=course.id,
            stadium_id=stadium.id,
            stadium_name=stadium.name_ko or stadium.name_jp,
            surface=MatchSurface(course.surface),
            distance=course.distance,
            direction=MatchDirection(course.direction),
            layout=StadiumCourseLayout(course.layout),
        ),
        condition=MatchOpeningCondition(
            season=MatchSeason(condition_row.season),
            weather=MatchWeather(condition_row.weather),
            time_of_day=MatchTimeOfDay(condition_row.time_of_day),
            track_condition=MatchTrackCondition(condition_row.track_condition),
        )
        if condition_row is not None
        else None,
        rating_rule_coverage=rating_rule_coverage,
        entries=entries,
        destination=destination,
    )


class SqlAlchemyMatchBettingOpenRepository:
    """Lock, transition, publish, and audit one native Match opening."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_target(self, *, match_id: int, guild_id: str) -> MatchBettingOpenTarget | None:
        return load_match_betting_open_target(
            self._session,
            match_id=match_id,
            guild_id=guild_id,
            lock=True,
        )

    def find_operation(self, *, idempotency_key: str) -> StoredMatchBettingOpenOperation | None:
        row = self._session.execute(
            select(
                OperationORM.request_fingerprint,
                MatchOperationORM.type,
                MatchOperationORM.match_id,
                MatchOperationORM.after_data,
            )
            .outerjoin(MatchOperationORM, MatchOperationORM.operation_id == OperationORM.id)
            .where(OperationORM.idempotency_key == idempotency_key)
            .with_for_update()
        ).one_or_none()
        if row is None:
            return None
        return StoredMatchBettingOpenOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            match_id=row.match_id,
            after_data=row.after_data,
        )

    def has_any_bet_facts(self, *, match_id: int) -> bool:
        return (
            self._session.scalar(select(BetORM.id).where(BetORM.match_id == match_id).with_for_update().limit(1))
            is not None
        )

    def transition_to_betting_open(self, *, match_id: int, changed_at: datetime) -> None:
        result = self._session.execute(
            update(MatchORM)
            .where(
                MatchORM.id == match_id,
                MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
                MatchORM.status == MatchStatus.SCHEDULED.value,
            )
            .values(
                status=MatchStatus.BETTING_OPEN.value,
                updated_at=to_database_utc(changed_at, field_name="changed_at"),
            )
        )
        if result.rowcount != 1:
            raise ValueError("Match status changed before the opening transition.")

    def start_periodic_odds_cycle_if_first_open(
        self,
        *,
        match_id: int,
        guild_id: str,
        opened_at: datetime,
    ) -> None:
        existing_open_match = self._session.scalar(
            select(MatchORM.id)
            .where(
                MatchORM.id != match_id,
                MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
                MatchORM.status == MatchStatus.BETTING_OPEN.value,
            )
            .limit(1)
        )
        if existing_open_match is not None:
            return
        self._session.execute(
            update(BotGuildSettingORM)
            .where(BotGuildSettingORM.guild_id == guild_id)
            .values(
                match_odds_refresh_mode=MatchOddsRefreshMode.NORMAL.value,
                match_odds_refresh_next_at=to_database_utc(
                    opened_at + MatchOddsRefreshMode.NORMAL.interval,
                    field_name="match_odds_refresh_next_at",
                ),
                match_odds_last_projection_fingerprint=None,
                updated_at=to_database_utc(opened_at, field_name="opened_at"),
            )
        )

    def add_publication(
        self,
        *,
        intent: PublicationIntent,
        created_at: datetime,
    ) -> StoredMatchOpeningPublication:
        stored_at = to_database_utc(created_at, field_name="created_at")
        publication = DiscordPublicationORM(
            guild_id=intent.guild_id,
            destination_kind=intent.destination_kind,
            event_type=intent.event_type,
            event_key=intent.event_key,
            source_kind=intent.source_kind,
            source_id=intent.source_id,
            target_channel_id=intent.target_channel_id,
            payload_json=intent.payload_json,
            payload_fingerprint=intent.payload_fingerprint,
            status=intent.status.value,
            attempt_count=0,
            discord_message_id=None,
            last_error_code=None,
            failure_stage=None,
            attempt_started_at=None,
            published_at=None,
            created_at=stored_at,
            updated_at=stored_at,
        )
        self._session.add(publication)
        self._session.flush()
        return StoredMatchOpeningPublication(
            publication_id=publication.id,
            event_key=publication.event_key,
            payload_fingerprint=publication.payload_fingerprint,
            status=intent.status,
            target_channel_id=publication.target_channel_id,
        )

    def add_audit(
        self,
        *,
        command: OpenMatchBetting,
        before: MatchBettingOpenTarget,
        after: OpenedMatchBetting,
        created_at: datetime,
    ) -> None:
        operation = OperationORM(
            guild_id=command.guild_id,
            correlation_id=command.correlation_id,
            actor_discord_user_id=command.actor_discord_user_id,
            idempotency_key=command.idempotency_key,
            request_fingerprint=command.request_fingerprint,
            reason=None,
            created_at=to_database_utc(created_at, field_name="created_at"),
        )
        self._session.add(operation)
        self._session.flush()
        self._session.add(
            MatchOperationORM(
                operation_id=operation.id,
                match_id=after.match_id,
                type="match_betting_opened",
                before_data=before.to_audit_payload(),
                after_data=after.to_audit_payload(),
            )
        )
        self._session.flush()


class SqlAlchemyMatchBettingOpenUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete feature UoW exposing Match betting-open mutation."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._match_betting_open: SqlAlchemyMatchBettingOpenRepository | None = None

    @property
    def match_betting_open(self) -> SqlAlchemyMatchBettingOpenRepository:
        return self._require_active_repository(self._match_betting_open)

    def _activate_repositories(self) -> None:
        self._match_betting_open = SqlAlchemyMatchBettingOpenRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._match_betting_open = None


class SqlAlchemyMatchBettingOpenUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchBettingOpenUnitOfWork]
):
    """Create one Match betting-open UoW per final command."""

    unit_of_work_type = SqlAlchemyMatchBettingOpenUnitOfWork
