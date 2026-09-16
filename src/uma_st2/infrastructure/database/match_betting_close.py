"""SQLAlchemy implementation of native Match betting-close."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from uma_st2.application.match.betting_close import (
    ClosedMatchBetting,
    CloseMatchBetting,
    MatchBettingCloseAuditType,
    MatchBettingCloseEntry,
    MatchBettingCloseTarget,
    StoredMatchBettingCloseOperation,
    StoredMatchBettingClosePublication,
)
from uma_st2.application.publication import PublicationIntent
from uma_st2.application.publication.match import MatchPublicationDestination
from uma_st2.domain.betting import BetPoolStake, BetStatus, BetType
from uma_st2.domain.match import MatchGrade, MatchSourceKind, MatchStatus
from uma_st2.domain.publication import PublicationStatus

from .datetime_codec import from_database_utc, to_database_utc
from .orm import (
    BetORM,
    BotGuildSettingORM,
    DiscordPublicationORM,
    MatchEntryORM,
    MatchOperationORM,
    MatchORM,
    OperationORM,
)
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


def load_match_betting_close_target(
    session: Session,
    *,
    match_id: int,
    guild_id: str,
    lock: bool,
) -> MatchBettingCloseTarget | None:
    """Load one Match first, then its complete current pool and destination."""

    match_statement = select(MatchORM).where(MatchORM.id == match_id)
    if lock:
        match_statement = match_statement.with_for_update()
    match = session.scalar(match_statement)
    if match is None:
        return None

    inconsistent_marker = session.scalar(
        select(BetORM.id)
        .where(
            BetORM.match_id == match_id,
            or_(
                and_(BetORM.status == "active", BetORM.active_marker.is_not(True)),
                and_(BetORM.status != "active", BetORM.active_marker.is_(True)),
            ),
        )
        .limit(1)
    )
    if inconsistent_marker is not None:
        raise ValueError("Bet status and active marker are inconsistent.")

    entries_statement = (
        select(MatchEntryORM.id, MatchEntryORM.entry_number)
        .where(MatchEntryORM.match_id == match_id)
        .order_by(MatchEntryORM.entry_number, MatchEntryORM.id)
    )
    active_bets_statement = (
        select(BetORM.type, BetORM.selections, BetORM.amount)
        .where(
            BetORM.match_id == match_id,
            BetORM.status == BetStatus.ACTIVE.value,
            BetORM.active_marker.is_(True),
        )
        .order_by(BetORM.id)
    )
    settings_statement = select(BotGuildSettingORM).where(BotGuildSettingORM.guild_id == guild_id)
    if lock:
        entries_statement = entries_statement.with_for_update()
        active_bets_statement = active_bets_statement.with_for_update()
        settings_statement = settings_statement.with_for_update()
    entry_rows = tuple(session.execute(entries_statement))
    active_bet_rows = tuple(session.execute(active_bets_statement))
    setting = session.scalar(settings_statement)

    entries = tuple(
        MatchBettingCloseEntry(entry_id=entry_id, entry_number=entry_number) for entry_id, entry_number in entry_rows
    )
    active_bets: list[BetPoolStake] = []
    for bet_type, raw_selections, amount in active_bet_rows:
        if not isinstance(raw_selections, list):
            raise ValueError("Active Bet selections must be a JSON array.")
        active_bets.append(
            BetPoolStake(
                bet_type=BetType(bet_type),
                selection_ids=tuple(raw_selections),
                amount=amount,
            )
        )
    return MatchBettingCloseTarget(
        match_id=match.id,
        match_name=match.name,
        source_kind=MatchSourceKind(match.source_kind),
        status=MatchStatus(match.status),
        grade=MatchGrade(match.grade),
        scheduled_at=from_database_utc(match.scheduled_at, field_name="matches.scheduled_at"),
        entry_count=len(entries),
        active_bet_count=len(active_bets),
        active_stake_total=sum(bet.amount for bet in active_bets),
        entries=entries,
        active_bets=tuple(active_bets),
        destination=MatchPublicationDestination(
            guild_id=guild_id,
            announcements_enabled=True if setting is None else setting.match_announcements_enabled,
            target_channel_id=None if setting is None else setting.match_announcement_channel_id,
        ),
    )


class SqlAlchemyMatchBettingCloseRepository:
    """Lock, transition, and audit one native Match close."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_target(self, *, match_id: int, guild_id: str) -> MatchBettingCloseTarget | None:
        return load_match_betting_close_target(
            self._session,
            match_id=match_id,
            guild_id=guild_id,
            lock=True,
        )

    def find_operation(self, *, idempotency_key: str) -> StoredMatchBettingCloseOperation | None:
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
        return StoredMatchBettingCloseOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            match_id=row.match_id,
            after_data=row.after_data,
        )

    def transition_to_betting_closed(self, *, match_id: int, changed_at: datetime) -> None:
        result = self._session.execute(
            update(MatchORM)
            .where(
                MatchORM.id == match_id,
                MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
                MatchORM.status == MatchStatus.BETTING_OPEN.value,
            )
            .values(
                status=MatchStatus.BETTING_CLOSED.value,
                updated_at=to_database_utc(changed_at, field_name="changed_at"),
            )
        )
        if result.rowcount != 1:
            raise ValueError("Match status changed before the close transition.")

    def add_publication(
        self,
        *,
        intent: PublicationIntent,
        created_at: datetime,
    ) -> StoredMatchBettingClosePublication:
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
        return StoredMatchBettingClosePublication(
            publication_id=publication.id,
            event_key=publication.event_key,
            payload_fingerprint=publication.payload_fingerprint,
            status=PublicationStatus(publication.status),
            target_channel_id=publication.target_channel_id,
        )

    def add_audit(
        self,
        *,
        command: CloseMatchBetting,
        before: MatchBettingCloseTarget,
        after: ClosedMatchBetting,
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
                type=MatchBettingCloseAuditType.CLOSED.value,
                before_data=before.to_audit_payload(),
                after_data=after.to_audit_payload(),
            )
        )
        self._session.flush()


class SqlAlchemyMatchBettingCloseUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete feature UoW exposing Match betting-close mutation."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._match_betting_close: SqlAlchemyMatchBettingCloseRepository | None = None

    @property
    def match_betting_close(self) -> SqlAlchemyMatchBettingCloseRepository:
        return self._require_active_repository(self._match_betting_close)

    def _activate_repositories(self) -> None:
        self._match_betting_close = SqlAlchemyMatchBettingCloseRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._match_betting_close = None


class SqlAlchemyMatchBettingCloseUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchBettingCloseUnitOfWork]
):
    """Create one Match betting-close UoW per final command."""

    unit_of_work_type = SqlAlchemyMatchBettingCloseUnitOfWork
