"""SQLAlchemy persistence for guild-wide periodic Match odds publication."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from uma_st2.application.match.odds_publication import (
    ChangedMatchOddsRefreshMode,
    ChangeMatchOddsRefreshMode,
    MatchOddsPublicationQueryRepository,
    MatchOddsRefreshCursor,
    MatchOddsRefreshEntry,
    MatchOddsRefreshStatus,
    MatchOddsRefreshTarget,
    MatchOddsRefreshTargetMatch,
    StoredMatchOddsModeOperation,
    StoredMatchOddsRefreshPublication,
)
from uma_st2.application.publication import PublicationIntent
from uma_st2.application.publication.match import MatchPublicationDestination
from uma_st2.application.publication.match_odds import MatchOddsRefreshMode
from uma_st2.domain.betting import BetPoolStake, BetStatus, BetType
from uma_st2.domain.match import MatchGrade, MatchSourceKind, MatchStatus
from uma_st2.domain.publication import PublicationStatus

from .datetime_codec import from_database_utc, to_database_utc
from .orm import (
    BetORM,
    BotGuildSettingORM,
    DiscordPublicationORM,
    MatchEntryORM,
    MatchORM,
    OperationORM,
    SettingsOperationORM,
)
from .uow import SessionFactory, SqlAlchemyFeatureUnitOfWork, SqlAlchemyFeatureUnitOfWorkFactory


def _cursor(setting: BotGuildSettingORM) -> MatchOddsRefreshCursor:
    return MatchOddsRefreshCursor(
        guild_id=setting.guild_id,
        mode=MatchOddsRefreshMode(setting.match_odds_refresh_mode),
        next_refresh_at=(
            from_database_utc(
                setting.match_odds_refresh_next_at,
                field_name="bot_guild_settings.match_odds_refresh_next_at",
            )
            if setting.match_odds_refresh_next_at is not None
            else None
        ),
        sequence=setting.match_odds_refresh_sequence,
        last_projection_fingerprint=setting.match_odds_last_projection_fingerprint,
        destination=MatchPublicationDestination(
            guild_id=setting.guild_id,
            announcements_enabled=setting.match_announcements_enabled,
            target_channel_id=setting.match_announcement_channel_id,
        ),
    )


def _open_matches_statement():
    return (
        select(MatchORM)
        .where(
            MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
            MatchORM.status == MatchStatus.BETTING_OPEN.value,
        )
        .order_by(MatchORM.scheduled_at, MatchORM.id)
    )


class SqlAlchemyMatchOddsPublicationRepository:
    """Lock current pools, advance one cursor, and append durable intents/audits."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_target(self, *, guild_id: str) -> MatchOddsRefreshTarget | None:
        matches = tuple(self._session.scalars(_open_matches_statement().with_for_update()))
        setting = self._session.scalar(
            select(BotGuildSettingORM).where(BotGuildSettingORM.guild_id == guild_id).with_for_update()
        )
        if setting is None:
            return None

        if not matches:
            # Recheck after the settings lock. A concurrent opening owns Match ->
            # setting order and either commits before this read or waits until this
            # empty-cycle reset has completed.
            matches = tuple(self._session.scalars(_open_matches_statement().with_for_update()))
        if not matches:
            return MatchOddsRefreshTarget(cursor=_cursor(setting), matches=())

        match_ids = tuple(match.id for match in matches)
        entry_rows = tuple(
            self._session.execute(
                select(MatchEntryORM.id, MatchEntryORM.match_id, MatchEntryORM.entry_number)
                .where(MatchEntryORM.match_id.in_(match_ids))
                .order_by(MatchEntryORM.match_id, MatchEntryORM.entry_number, MatchEntryORM.id)
                .with_for_update()
            )
        )
        inconsistent_bet = self._session.scalar(
            select(BetORM.id)
            .where(
                BetORM.match_id.in_(match_ids),
                or_(
                    and_(
                        BetORM.status == BetStatus.ACTIVE.value,
                        BetORM.active_marker.is_not(True),
                    ),
                    and_(
                        BetORM.status != BetStatus.ACTIVE.value,
                        BetORM.active_marker.is_(True),
                    ),
                ),
            )
            .with_for_update()
            .limit(1)
        )
        if inconsistent_bet is not None:
            raise ValueError("Bet status and active marker are inconsistent.")
        bet_rows = tuple(
            self._session.execute(
                select(BetORM.match_id, BetORM.type, BetORM.selections, BetORM.amount)
                .where(
                    BetORM.match_id.in_(match_ids),
                    BetORM.status == BetStatus.ACTIVE.value,
                    BetORM.active_marker.is_(True),
                )
                .order_by(BetORM.match_id, BetORM.id)
                .with_for_update()
            )
        )

        entries_by_match: dict[int, list[MatchOddsRefreshEntry]] = defaultdict(list)
        for entry_id, match_id, entry_number in entry_rows:
            entries_by_match[match_id].append(MatchOddsRefreshEntry(entry_id=entry_id, entry_number=entry_number))
        bets_by_match: dict[int, list[BetPoolStake]] = defaultdict(list)
        for match_id, bet_type, raw_selections, amount in bet_rows:
            if not isinstance(raw_selections, list):
                raise ValueError("Active Bet selections must be a JSON array.")
            bets_by_match[match_id].append(
                BetPoolStake(
                    bet_type=BetType(bet_type),
                    selection_ids=tuple(raw_selections),
                    amount=amount,
                )
            )

        return MatchOddsRefreshTarget(
            cursor=_cursor(setting),
            matches=tuple(
                MatchOddsRefreshTargetMatch(
                    match_id=match.id,
                    match_name=match.name,
                    source_kind=MatchSourceKind(match.source_kind),
                    status=MatchStatus(match.status),
                    grade=MatchGrade(match.grade),
                    scheduled_at=from_database_utc(
                        match.scheduled_at,
                        field_name="matches.scheduled_at",
                    ),
                    entries=tuple(entries_by_match[match.id]),
                    active_bets=tuple(bets_by_match[match.id]),
                )
                for match in matches
            ),
        )

    def find_operation(self, *, idempotency_key: str) -> StoredMatchOddsModeOperation | None:
        row = self._session.execute(
            select(
                OperationORM.request_fingerprint,
                SettingsOperationORM.type,
                SettingsOperationORM.guild_id,
                SettingsOperationORM.after_data,
            )
            .outerjoin(SettingsOperationORM, SettingsOperationORM.operation_id == OperationORM.id)
            .where(OperationORM.idempotency_key == idempotency_key)
            .with_for_update()
        ).one_or_none()
        if row is None:
            return None
        return StoredMatchOddsModeOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            guild_id=row.guild_id,
            after_data=row.after_data,
        )

    def update_cursor(self, *, cursor: MatchOddsRefreshCursor, changed_at: datetime) -> None:
        result = self._session.execute(
            update(BotGuildSettingORM)
            .where(BotGuildSettingORM.guild_id == cursor.guild_id)
            .values(
                match_odds_refresh_mode=cursor.mode.value,
                match_odds_refresh_next_at=(
                    to_database_utc(cursor.next_refresh_at, field_name="next_refresh_at")
                    if cursor.next_refresh_at is not None
                    else None
                ),
                match_odds_refresh_sequence=cursor.sequence,
                match_odds_last_projection_fingerprint=cursor.last_projection_fingerprint,
                updated_at=to_database_utc(changed_at, field_name="changed_at"),
            )
        )
        if result.rowcount != 1:
            raise ValueError("Periodic Match odds cursor changed concurrently.")

    def add_publication(
        self,
        *,
        intent: PublicationIntent,
        created_at: datetime,
    ) -> StoredMatchOddsRefreshPublication:
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
        return StoredMatchOddsRefreshPublication(
            publication_id=publication.id,
            event_key=publication.event_key,
            payload_fingerprint=publication.payload_fingerprint,
            status=PublicationStatus(publication.status),
            target_channel_id=publication.target_channel_id,
        )

    def add_mode_audit(
        self,
        *,
        command: ChangeMatchOddsRefreshMode,
        before: MatchOddsRefreshCursor,
        after: ChangedMatchOddsRefreshMode,
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
            SettingsOperationORM(
                operation_id=operation.id,
                guild_id=command.guild_id,
                type="match_odds_refresh_mode_changed",
                before_data={
                    "schema_version": 1,
                    "cursor": before.to_payload(),
                },
                after_data=after.to_payload(),
            )
        )
        self._session.flush()


class SqlAlchemyMatchOddsPublicationQueryRepository(MatchOddsPublicationQueryRepository):
    """Read detached current mode/open-count status for the private View."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_status(self, *, guild_id: str) -> MatchOddsRefreshStatus | None:
        setting = self._session.get(BotGuildSettingORM, guild_id)
        if setting is None:
            return None
        open_match_count = self._session.scalar(
            select(func.count(MatchORM.id)).where(
                MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
                MatchORM.status == MatchStatus.BETTING_OPEN.value,
            )
        )
        return MatchOddsRefreshStatus(
            guild_id=setting.guild_id,
            mode=MatchOddsRefreshMode(setting.match_odds_refresh_mode),
            next_refresh_at=(
                from_database_utc(
                    setting.match_odds_refresh_next_at,
                    field_name="bot_guild_settings.match_odds_refresh_next_at",
                )
                if setting.match_odds_refresh_next_at is not None
                else None
            ),
            open_match_count=int(open_match_count or 0),
        )


class SqlAlchemyMatchOddsPublicationUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._match_odds_publication: SqlAlchemyMatchOddsPublicationRepository | None = None

    @property
    def match_odds_publication(self) -> SqlAlchemyMatchOddsPublicationRepository:
        return self._require_active_repository(self._match_odds_publication)

    def _activate_repositories(self) -> None:
        self._match_odds_publication = SqlAlchemyMatchOddsPublicationRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._match_odds_publication = None


class SqlAlchemyMatchOddsPublicationUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchOddsPublicationUnitOfWork]
):
    unit_of_work_type = SqlAlchemyMatchOddsPublicationUnitOfWork


class SqlAlchemyMatchOddsPublicationQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._queries: SqlAlchemyMatchOddsPublicationQueryRepository | None = None

    @property
    def match_odds_publication_queries(self) -> SqlAlchemyMatchOddsPublicationQueryRepository:
        return self._require_active_repository(self._queries)

    def _activate_repositories(self) -> None:
        self._queries = SqlAlchemyMatchOddsPublicationQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._queries = None


class SqlAlchemyMatchOddsPublicationQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchOddsPublicationQueryUnitOfWork]
):
    unit_of_work_type = SqlAlchemyMatchOddsPublicationQueryUnitOfWork
