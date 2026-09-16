"""SQLAlchemy member query projections for native Match Bet placement."""

from __future__ import annotations

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from uma_st2.application.betting import (
    ActiveMatchBetChoice,
    MatchBetTargetChoice,
    MatchRaceDetail,
    MatchRaceDetailCondition,
    MatchRaceDetailCourse,
    MatchRaceDetailEntry,
    MatchRaceListItem,
    MemberMatchBetHistoryItem,
    fingerprint_bet_selection,
)
from uma_st2.domain.betting import BetStatus, BetType, canonicalize_selections
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

from .datetime_codec import from_database_utc
from .orm import (
    BetORM,
    CirclePointORM,
    DiscordAccountORM,
    GameAccountORM,
    MatchConditionORM,
    MatchEntryORM,
    MatchORM,
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


class SqlAlchemyMatchMemberBettingQueryRepository:
    """Build bounded current Match lists, betting choices, and owned Bet history."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchBetTargetChoice, ...]:
        entry_count = (
            select(func.count(MatchEntryORM.id))
            .where(MatchEntryORM.match_id == MatchORM.id)
            .correlate(MatchORM)
            .scalar_subquery()
        )
        statement = select(
            MatchORM.id,
            MatchORM.name,
            MatchORM.grade,
            MatchORM.scheduled_at,
            entry_count.label("entry_count"),
        ).where(
            MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
            MatchORM.status == MatchStatus.BETTING_OPEN.value,
            entry_count > 0,
        )
        if search:
            statement = statement.where(MatchORM.name.contains(search))
        rows = self._session.execute(statement.order_by(MatchORM.scheduled_at, MatchORM.id).limit(limit))
        return tuple(
            MatchBetTargetChoice(
                match_id=row.id,
                match_name=row.name,
                grade=row.grade,
                scheduled_at=from_database_utc(row.scheduled_at, field_name="matches.scheduled_at"),
                entry_count=row.entry_count,
            )
            for row in rows
        )

    def get_current_balance(self, *, actor_discord_user_id: str) -> int | None:
        return self._session.scalar(
            select(CirclePointORM.balance)
            .join(DiscordAccountORM, DiscordAccountORM.persona_id == CirclePointORM.persona_id)
            .where(DiscordAccountORM.discord_user_id == actor_discord_user_id)
        )

    def list_races(self, *, limit: int) -> tuple[MatchRaceListItem, ...]:
        rows = self._session.execute(self._race_list_statement().limit(limit))
        return self._race_list_items(rows)

    def search_races(self, *, search: str, limit: int) -> tuple[MatchRaceListItem, ...]:
        statement = self._race_list_statement()
        if search:
            statement = statement.where(MatchORM.name.contains(search, autoescape=True))
        rows = self._session.execute(statement.limit(limit))
        return self._race_list_items(rows)

    def get_race_detail(self, *, match_id: int) -> MatchRaceDetail | None:
        row = self._session.execute(
            select(MatchORM, StadiumCourseORM, StadiumORM, MatchConditionORM)
            .join(StadiumCourseORM, StadiumCourseORM.id == MatchORM.stadium_course_id)
            .join(StadiumORM, StadiumORM.id == StadiumCourseORM.stadium_id)
            .outerjoin(MatchConditionORM, MatchConditionORM.match_id == MatchORM.id)
            .where(
                MatchORM.id == match_id,
                *self._race_scope(),
            )
        ).one_or_none()
        if row is None:
            return None
        match, course, stadium, condition = row
        if condition is None:
            raise ValueError("A current native Match must have a complete condition.")

        variant = UmamusumeVariantORM
        entry_rows = self._session.execute(
            select(MatchEntryORM, GameAccountORM, UmamusumeORM, variant)
            .join(GameAccountORM, GameAccountORM.id == MatchEntryORM.game_account_id)
            .join(UmamusumeORM, UmamusumeORM.id == MatchEntryORM.umamusume_id)
            .outerjoin(variant, variant.id == MatchEntryORM.umamusume_variant_id)
            .where(MatchEntryORM.match_id == match_id)
            .order_by(MatchEntryORM.entry_number, MatchEntryORM.id)
        )
        return MatchRaceDetail(
            match_id=match.id,
            match_name=match.name,
            description=match.description,
            grade=MatchGrade(match.grade),
            scheduled_at=from_database_utc(match.scheduled_at, field_name="matches.scheduled_at"),
            status=MatchStatus(match.status),
            course=MatchRaceDetailCourse(
                stadium_name=stadium.name_ko or stadium.name_jp,
                surface=MatchSurface(course.surface),
                distance=course.distance,
                direction=MatchDirection(course.direction),
                layout=StadiumCourseLayout(course.layout),
            ),
            condition=MatchRaceDetailCondition(
                season=MatchSeason(condition.season),
                weather=MatchWeather(condition.weather),
                time_of_day=MatchTimeOfDay(condition.time_of_day),
                track_condition=MatchTrackCondition(condition.track_condition),
            ),
            entries=tuple(
                MatchRaceDetailEntry(
                    entry_number=entry.entry_number,
                    game_account_name=account.nickname,
                    umamusume_name=(variant_row.name_ko or variant_row.name_jp)
                    if variant_row is not None
                    else (base.name_ko or base.name_jp),
                    affiliation=entry.affiliation_at_event,
                )
                for entry, account, base, variant_row in entry_rows
            ),
        )

    @staticmethod
    def _race_scope() -> tuple[object, ...]:
        return (
            MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
            MatchORM.status.in_((MatchStatus.SCHEDULED.value, MatchStatus.BETTING_OPEN.value)),
        )

    @classmethod
    def _race_list_statement(cls):
        entry_count = (
            select(func.count(MatchEntryORM.id))
            .where(MatchEntryORM.match_id == MatchORM.id)
            .correlate(MatchORM)
            .scalar_subquery()
        )
        return (
            select(
                MatchORM.id,
                MatchORM.name,
                MatchORM.grade,
                MatchORM.scheduled_at,
                MatchORM.status,
                entry_count.label("entry_count"),
            )
            .where(*cls._race_scope())
            .order_by(MatchORM.scheduled_at, MatchORM.id)
        )

    @staticmethod
    def _race_list_items(rows) -> tuple[MatchRaceListItem, ...]:
        return tuple(
            MatchRaceListItem(
                match_id=row.id,
                match_name=row.name,
                grade=row.grade,
                scheduled_at=from_database_utc(row.scheduled_at, field_name="matches.scheduled_at"),
                status=row.status,
                entry_count=row.entry_count,
            )
            for row in rows
        )

    def search_active_bets(
        self,
        *,
        actor_discord_user_id: str,
        search: str,
        limit: int,
    ) -> tuple[ActiveMatchBetChoice, ...]:
        persona_id = self._session.scalar(
            select(DiscordAccountORM.persona_id).where(DiscordAccountORM.discord_user_id == actor_discord_user_id)
        )
        if persona_id is None:
            return ()

        match_scope = (
            BetORM.persona_id == persona_id,
            MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
            MatchORM.status == MatchStatus.BETTING_OPEN.value,
        )
        inconsistent = self._session.scalar(
            select(BetORM.id)
            .join(MatchORM, MatchORM.id == BetORM.match_id)
            .where(
                *match_scope,
                or_(
                    and_(BetORM.status == BetStatus.ACTIVE.value, BetORM.active_marker.is_not(True)),
                    and_(BetORM.status != BetStatus.ACTIVE.value, BetORM.active_marker.is_(True)),
                ),
            )
            .limit(1)
        )
        if inconsistent is not None:
            raise ValueError("Bet status and active marker are inconsistent.")

        statement = (
            select(
                BetORM.id,
                BetORM.match_id,
                BetORM.type,
                BetORM.selections,
                BetORM.selection_fingerprint,
                BetORM.amount,
                MatchORM.name.label("match_name"),
                MatchORM.scheduled_at,
            )
            .join(MatchORM, MatchORM.id == BetORM.match_id)
            .where(
                *match_scope,
                BetORM.status == BetStatus.ACTIVE.value,
                BetORM.active_marker.is_(True),
            )
        )
        if search:
            filters = [MatchORM.name.contains(search)]
            if search.isascii() and search.isdigit() and int(search) > 0:
                filters.append(BetORM.id == int(search))
            statement = statement.where(or_(*filters))
        rows = tuple(
            self._session.execute(statement.order_by(MatchORM.scheduled_at, MatchORM.id, BetORM.id).limit(limit))
        )
        entry_numbers_by_bet = self._validated_entry_numbers(rows)
        return tuple(
            ActiveMatchBetChoice(
                bet_id=row.id,
                match_id=row.match_id,
                match_name=row.match_name,
                scheduled_at=from_database_utc(row.scheduled_at, field_name="matches.scheduled_at"),
                bet_type=BetType(row.type),
                entry_numbers=entry_numbers_by_bet[row.id],
                selection_fingerprint=row.selection_fingerprint,
                amount=row.amount,
            )
            for row in rows
        )

    def list_personal_bets(
        self,
        *,
        actor_discord_user_id: str,
        limit: int,
    ) -> tuple[MemberMatchBetHistoryItem, ...]:
        persona_id = self._session.scalar(
            select(DiscordAccountORM.persona_id).where(DiscordAccountORM.discord_user_id == actor_discord_user_id)
        )
        if persona_id is None:
            return ()

        scope = (
            BetORM.persona_id == persona_id,
            MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
        )
        inconsistent = self._session.scalar(
            select(BetORM.id)
            .join(MatchORM, MatchORM.id == BetORM.match_id)
            .where(
                *scope,
                or_(
                    and_(BetORM.status == BetStatus.ACTIVE.value, BetORM.active_marker.is_not(True)),
                    and_(BetORM.status != BetStatus.ACTIVE.value, BetORM.active_marker.is_(True)),
                ),
            )
            .limit(1)
        )
        if inconsistent is not None:
            raise ValueError("Bet status and active marker are inconsistent.")

        rows = tuple(
            self._session.execute(
                select(
                    BetORM.id,
                    BetORM.match_id,
                    BetORM.type,
                    BetORM.selections,
                    BetORM.selection_fingerprint,
                    BetORM.amount,
                    BetORM.status.label("bet_status"),
                    BetORM.created_at,
                    MatchORM.name.label("match_name"),
                    MatchORM.scheduled_at,
                    MatchORM.status.label("match_status"),
                )
                .join(MatchORM, MatchORM.id == BetORM.match_id)
                .where(*scope)
                .order_by(BetORM.created_at.desc(), BetORM.id.desc())
                .limit(limit)
            )
        )
        entry_numbers_by_bet = self._validated_entry_numbers(rows)
        return tuple(
            MemberMatchBetHistoryItem(
                bet_id=row.id,
                match_name=row.match_name,
                scheduled_at=from_database_utc(row.scheduled_at, field_name="matches.scheduled_at"),
                match_status=MatchStatus(row.match_status),
                bet_type=BetType(row.type),
                entry_numbers=entry_numbers_by_bet[row.id],
                amount=row.amount,
                bet_status=BetStatus(row.bet_status),
                created_at=from_database_utc(row.created_at, field_name="bets.created_at"),
            )
            for row in rows
        )

    def _validated_entry_numbers(self, rows: tuple[object, ...]) -> dict[int, tuple[int, ...]]:
        selection_ids_by_bet: dict[int, tuple[int, ...]] = {}
        all_selection_ids: set[int] = set()
        for row in rows:
            if not isinstance(row.selections, list):
                raise ValueError("Bet selections must be stored as a JSON array.")
            bet_type = BetType(row.type)
            raw_selection_ids = tuple(row.selections)
            selection_ids = canonicalize_selections(bet_type, raw_selection_ids)
            if raw_selection_ids != selection_ids:
                raise ValueError("Bet selections must use canonical Entry ID order.")
            if row.selection_fingerprint != fingerprint_bet_selection(
                bet_type=bet_type,
                selection_ids=selection_ids,
            ):
                raise ValueError("Bet selection fingerprint is inconsistent.")
            selection_ids_by_bet[row.id] = selection_ids
            all_selection_ids.update(selection_ids)

        entry_rows = (
            tuple(
                self._session.execute(
                    select(MatchEntryORM.id, MatchEntryORM.match_id, MatchEntryORM.entry_number).where(
                        MatchEntryORM.id.in_(all_selection_ids)
                    )
                )
            )
            if all_selection_ids
            else ()
        )
        entries_by_id = {row.id: row for row in entry_rows}
        entry_numbers_by_bet: dict[int, tuple[int, ...]] = {}
        for row in rows:
            selection_ids = selection_ids_by_bet[row.id]
            try:
                selected_entries = tuple(entries_by_id[entry_id] for entry_id in selection_ids)
            except KeyError as exc:
                raise ValueError("Bet references a missing Match Entry.") from exc
            if any(entry.match_id != row.match_id for entry in selected_entries):
                raise ValueError("Bet selection references another Match.")
            entry_numbers_by_bet[row.id] = canonicalize_selections(
                BetType(row.type),
                tuple(entry.entry_number for entry in selected_entries),
            )
        return entry_numbers_by_bet


class SqlAlchemyMatchMemberBettingQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW for member Bet target projections."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._queries: SqlAlchemyMatchMemberBettingQueryRepository | None = None

    @property
    def match_member_betting_queries(self) -> SqlAlchemyMatchMemberBettingQueryRepository:
        return self._require_active_repository(self._queries)

    def _activate_repositories(self) -> None:
        self._queries = SqlAlchemyMatchMemberBettingQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._queries = None


class SqlAlchemyMatchMemberBettingQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchMemberBettingQueryUnitOfWork]
):
    """Create one read-only member Bet query UoW per operation."""

    unit_of_work_type = SqlAlchemyMatchMemberBettingQueryUnitOfWork
