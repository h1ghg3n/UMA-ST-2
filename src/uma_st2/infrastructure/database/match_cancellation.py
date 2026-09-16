"""SQLAlchemy implementation of native whole-Match cancellation."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from uma_st2.application.match.cancellation import (
    MATCH_BET_REFUND_POINT_ACTION,
    CancelledMatch,
    CancelMatch,
    MatchCancellationAuditType,
    MatchCancellationBet,
    MatchCancellationRefund,
    MatchCancellationRefundPlan,
    MatchCancellationTarget,
    MatchCancellationWallet,
    MatchCancellationWalletUnavailableError,
    StoredMatchCancellationOperation,
    StoredMatchRefundPublication,
)
from uma_st2.application.publication import PublicationIntent
from uma_st2.application.publication.match import MatchPublicationDestination
from uma_st2.domain.betting import BetStatus
from uma_st2.domain.match import MatchGrade, MatchSourceKind, MatchStatus

from .datetime_codec import from_database_utc, to_database_utc
from .orm import (
    BetORM,
    BotGuildSettingORM,
    CirclePointORM,
    DiscordPublicationORM,
    MatchEntryORM,
    MatchOperationORM,
    MatchORM,
    OperationORM,
    PointTransactionORM,
)
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


def _integer_aggregate(value: object, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer aggregate.")
    try:
        converted = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be an integer aggregate.") from exc
    if converted != value:
        raise ValueError(f"{field_name} must be an integer aggregate.")
    return converted


def _validate_bet_state(session: Session, *, match_id: int) -> None:
    inconsistent_marker = session.scalar(
        select(BetORM.id)
        .where(
            BetORM.match_id == match_id,
            or_(
                and_(BetORM.status == BetStatus.ACTIVE.value, BetORM.active_marker.is_not(True)),
                and_(BetORM.status != BetStatus.ACTIVE.value, BetORM.active_marker.is_(True)),
            ),
        )
        .limit(1)
    )
    if inconsistent_marker is not None:
        raise ValueError("Bet status and active marker are inconsistent.")
    settled_bet = session.scalar(
        select(BetORM.id).where(BetORM.match_id == match_id, BetORM.status == BetStatus.SETTLED.value).limit(1)
    )
    if settled_bet is not None:
        raise ValueError("A pre-settlement cancellation target contains a settled Bet.")


class SqlAlchemyMatchCancellationRepository:
    """Lock Match-first authority and persist terminal refunds atomically."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_target(self, *, match_id: int, guild_id: str) -> MatchCancellationTarget | None:
        match = self._session.scalar(select(MatchORM).where(MatchORM.id == match_id).with_for_update())
        if match is None:
            return None

        bet_rows = tuple(
            self._session.scalars(
                select(BetORM).where(BetORM.match_id == match_id).order_by(BetORM.id).with_for_update()
            )
        )
        _validate_bet_state(self._session, match_id=match_id)
        active_bets = tuple(
            MatchCancellationBet(id=bet.id, persona_id=bet.persona_id, amount=bet.amount)
            for bet in bet_rows
            if bet.status == BetStatus.ACTIVE.value and bet.active_marker is True
        )
        persona_ids = tuple(sorted({bet.persona_id for bet in active_bets}))
        wallet_rows = (
            tuple(
                self._session.scalars(
                    select(CirclePointORM)
                    .where(CirclePointORM.persona_id.in_(persona_ids))
                    .order_by(CirclePointORM.persona_id)
                    .with_for_update()
                )
            )
            if persona_ids
            else ()
        )
        if tuple(wallet.persona_id for wallet in wallet_rows) != persona_ids:
            raise MatchCancellationWalletUnavailableError(
                "Every active-Bet Persona must retain a canonical Circle Point wallet."
            )
        entry_count = self._session.scalar(
            select(func.count(MatchEntryORM.id)).where(MatchEntryORM.match_id == match_id)
        )
        setting = self._session.scalar(
            select(BotGuildSettingORM).where(BotGuildSettingORM.guild_id == guild_id).with_for_update()
        )
        return MatchCancellationTarget(
            match_id=match.id,
            match_name=match.name,
            source_kind=MatchSourceKind(match.source_kind),
            status=MatchStatus(match.status),
            terminal_reason=match.terminal_reason,
            grade=MatchGrade(match.grade),
            scheduled_at=from_database_utc(match.scheduled_at, field_name="matches.scheduled_at"),
            entry_count=_integer_aggregate(entry_count, field_name="entry_count"),
            active_bets=active_bets,
            wallets=tuple(
                MatchCancellationWallet(persona_id=wallet.persona_id, balance=wallet.balance) for wallet in wallet_rows
            ),
            destination=MatchPublicationDestination(
                guild_id=guild_id,
                announcements_enabled=True if setting is None else setting.match_announcements_enabled,
                target_channel_id=None if setting is None else setting.match_announcement_channel_id,
            ),
        )

    def find_operation(self, *, idempotency_key: str) -> StoredMatchCancellationOperation | None:
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
        return StoredMatchCancellationOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            match_id=row.match_id,
            after_data=row.after_data,
        )

    def cancel_and_refund(
        self,
        *,
        command: CancelMatch,
        target: MatchCancellationTarget,
        refund_plans: tuple[MatchCancellationRefundPlan, ...],
        publication_intent: PublicationIntent | None,
        cancelled_at: datetime,
    ) -> CancelledMatch:
        stored_time = to_database_utc(cancelled_at, field_name="cancelled_at")
        operation = OperationORM(
            guild_id=command.guild_id,
            correlation_id=command.correlation_id,
            actor_discord_user_id=command.actor_discord_user_id,
            idempotency_key=command.idempotency_key,
            request_fingerprint=command.request_fingerprint,
            reason=command.reason,
            created_at=stored_time,
        )
        self._session.add(operation)
        self._session.flush()

        changed_match = self._session.execute(
            update(MatchORM)
            .where(
                MatchORM.id == target.match_id,
                MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
                MatchORM.status == target.status.value,
                MatchORM.terminal_reason.is_(None),
            )
            .values(
                status=MatchStatus.CANCELLED.value,
                terminal_reason=command.reason,
                updated_at=stored_time,
            )
        )
        if changed_match.rowcount != 1:
            raise ValueError("Match changed before terminal cancellation.")

        active_bet_ids = tuple(bet.id for bet in target.active_bets)
        if active_bet_ids:
            changed_bets = self._session.execute(
                update(BetORM)
                .where(
                    BetORM.id.in_(active_bet_ids),
                    BetORM.match_id == target.match_id,
                    BetORM.status == BetStatus.ACTIVE.value,
                    BetORM.active_marker.is_(True),
                )
                .values(
                    status=BetStatus.CANCELLED.value,
                    active_marker=None,
                    updated_at=stored_time,
                )
            )
            if changed_bets.rowcount != len(active_bet_ids):
                raise ValueError("Active Bet set changed before cancellation.")

        refunds: list[MatchCancellationRefund] = []
        for plan in refund_plans:
            changed_wallet = self._session.execute(
                update(CirclePointORM)
                .where(
                    CirclePointORM.persona_id == plan.persona_id,
                    CirclePointORM.balance == plan.balance_before,
                )
                .values(balance=plan.balance_after, updated_at=stored_time)
            )
            if changed_wallet.rowcount != 1:
                raise ValueError("Locked Circle Point wallet changed before refund.")
            point_transaction = PointTransactionORM(
                persona_id=plan.persona_id,
                operation_id=operation.id,
                action=MATCH_BET_REFUND_POINT_ACTION,
                amount=plan.amount,
                created_at=stored_time,
            )
            self._session.add(point_transaction)
            self._session.flush()
            refunds.append(
                MatchCancellationRefund(
                    persona_id=plan.persona_id,
                    bet_ids=plan.bet_ids,
                    amount=plan.amount,
                    balance_before=plan.balance_before,
                    balance_after=plan.balance_after,
                    point_transaction_id=point_transaction.id,
                )
            )

        publication = (
            self._add_publication(intent=publication_intent, created_at=cancelled_at)
            if publication_intent is not None
            else None
        )

        cancelled = CancelledMatch(
            match_id=target.match_id,
            match_name=target.match_name,
            previous_status=target.status,
            status=MatchStatus.CANCELLED,
            reason=command.reason,
            cancelled_at=cancelled_at,
            entry_count=target.entry_count,
            cancelled_bet_count=len(active_bet_ids),
            refund_total=sum(refund.amount for refund in refunds),
            refunds=tuple(refunds),
            publication=publication,
        )
        self._session.add(
            MatchOperationORM(
                operation_id=operation.id,
                match_id=target.match_id,
                type=MatchCancellationAuditType.CANCELLED.value,
                before_data=target.to_audit_payload(),
                after_data=cancelled.to_audit_payload(),
            )
        )
        self._session.flush()
        return cancelled

    def _add_publication(
        self,
        *,
        intent: PublicationIntent,
        created_at: datetime,
    ) -> StoredMatchRefundPublication:
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
        return StoredMatchRefundPublication(
            publication_id=publication.id,
            event_key=publication.event_key,
            payload_fingerprint=publication.payload_fingerprint,
            status=intent.status,
            target_channel_id=publication.target_channel_id,
        )


class SqlAlchemyMatchCancellationUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete feature UoW exposing whole-Match cancellation."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._match_cancellation: SqlAlchemyMatchCancellationRepository | None = None

    @property
    def match_cancellation(self) -> SqlAlchemyMatchCancellationRepository:
        return self._require_active_repository(self._match_cancellation)

    def _activate_repositories(self) -> None:
        self._match_cancellation = SqlAlchemyMatchCancellationRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._match_cancellation = None


class SqlAlchemyMatchCancellationUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchCancellationUnitOfWork]
):
    """Create one whole-Match cancellation UoW per final command."""

    unit_of_work_type = SqlAlchemyMatchCancellationUnitOfWork
