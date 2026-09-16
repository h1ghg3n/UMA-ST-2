"""SQLAlchemy implementation of terminal native Match settlement rollback."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from uma_st2.application.match import (
    MATCH_BET_PAYOUT_REVERSAL_POINT_ACTION,
    MATCH_BET_REFUND_POINT_ACTION,
    MATCH_PLACEMENT_REWARD_REVERSAL_POINT_ACTION,
    MATCH_SETTLEMENT_ROLLBACK_AUDIT_SCHEMA_VERSION,
    MatchSettlementAuditType,
    MatchSettlementOriginalPointTransaction,
    MatchSettlementOriginalRatingTransaction,
    MatchSettlementPointCompensation,
    MatchSettlementRatingCompensation,
    MatchSettlementRollbackAuditType,
    MatchSettlementRollbackBet,
    MatchSettlementRollbackEvidenceExpiredError,
    MatchSettlementRollbackLock,
    MatchSettlementRollbackPlan,
    MatchSettlementRollbackTarget,
    MatchSettlementRollbackTargetChoice,
    MatchSettlementRollbackWallet,
    RollbackMatchSettlement,
    RolledBackMatchSettlement,
    SettledMatch,
    StoredMatchSettlementRollbackOperation,
)
from uma_st2.application.publication import MatchSettlementVoidedPublicationSource, PublicationIntent
from uma_st2.application.publication.match import MatchPublicationDestination
from uma_st2.domain.betting import BetStatus
from uma_st2.domain.match import MatchGrade, MatchSourceKind, MatchStatus

from .datetime_codec import from_database_utc, to_database_utc
from .match_publication import SqlAlchemyMatchResultPublicationRepository
from .orm import (
    BetORM,
    BotGuildSettingORM,
    CirclePointORM,
    GameAccountORM,
    MatchEntryORM,
    MatchOperationORM,
    MatchORM,
    OperationORM,
    PointTransactionORM,
    RatingORM,
    RatingTransactionORM,
)
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


def _integer_value(value: object, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer.")
    try:
        converted = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc
    if converted != value:
        raise ValueError(f"{field_name} must be an integer.")
    return converted


def _load_original_settlement(
    session: Session,
    *,
    match_id: int,
    lock: bool,
) -> tuple[int, SettledMatch]:
    statement = (
        select(MatchOperationORM.operation_id, MatchOperationORM.after_data)
        .where(
            MatchOperationORM.match_id == match_id,
            MatchOperationORM.type == MatchSettlementAuditType.SETTLED.value,
        )
        .order_by(MatchOperationORM.operation_id)
    )
    if lock:
        statement = statement.with_for_update()
    rows = tuple(session.execute(statement))
    if len(rows) != 1:
        raise MatchSettlementRollbackEvidenceExpiredError(
            "operation evidence expired: exactly one settlement audit is required"
        )
    operation_id, after_data = rows[0]
    if not isinstance(after_data, dict):
        raise MatchSettlementRollbackEvidenceExpiredError(
            "operation evidence expired: settlement audit payload is missing"
        )
    try:
        settlement = SettledMatch.from_audit_payload(after_data)
    except (KeyError, TypeError, ValueError) as exc:
        raise MatchSettlementRollbackEvidenceExpiredError(
            "operation evidence expired: settlement audit payload is unsupported"
        ) from exc
    return operation_id, settlement


def _load_bets(
    session: Session,
    *,
    match_id: int,
    expected_ids: tuple[int, ...],
    lock: bool,
) -> tuple[MatchSettlementRollbackBet, ...]:
    statement = select(BetORM).where(BetORM.match_id == match_id).order_by(BetORM.id)
    if lock:
        statement = statement.with_for_update()
    rows = tuple(session.scalars(statement))
    if tuple(row.id for row in rows) != expected_ids or any(
        row.status != BetStatus.SETTLED.value or row.active_marker is not None for row in rows
    ):
        raise MatchSettlementRollbackEvidenceExpiredError(
            "operation evidence expired: settled Bet coverage is inconsistent"
        )
    return tuple(
        MatchSettlementRollbackBet(
            bet_id=row.id,
            persona_id=row.persona_id,
            amount=row.amount,
        )
        for row in rows
    )


def _load_wallets(
    session: Session,
    *,
    persona_ids: tuple[str, ...],
    lock: bool,
) -> tuple[MatchSettlementRollbackWallet, ...]:
    if not persona_ids:
        return ()
    statement = (
        select(CirclePointORM).where(CirclePointORM.persona_id.in_(persona_ids)).order_by(CirclePointORM.persona_id)
    )
    if lock:
        statement = statement.with_for_update()
    rows = tuple(session.scalars(statement))
    if tuple(row.persona_id for row in rows) != persona_ids:
        raise MatchSettlementRollbackEvidenceExpiredError(
            "operation evidence expired: a referenced Circle Point wallet is missing"
        )
    return tuple(MatchSettlementRollbackWallet(row.persona_id, row.balance) for row in rows)


def _load_point_transactions(
    session: Session,
    *,
    transaction_ids: tuple[int, ...],
    lock: bool,
) -> tuple[MatchSettlementOriginalPointTransaction, ...]:
    if not transaction_ids:
        return ()
    statement = (
        select(PointTransactionORM).where(PointTransactionORM.id.in_(transaction_ids)).order_by(PointTransactionORM.id)
    )
    if lock:
        statement = statement.with_for_update()
    rows = tuple(session.scalars(statement))
    if tuple(row.id for row in rows) != transaction_ids:
        raise MatchSettlementRollbackEvidenceExpiredError(
            "operation evidence expired: a referenced Point transaction is missing"
        )
    return tuple(
        MatchSettlementOriginalPointTransaction(
            transaction_id=row.id,
            operation_id=row.operation_id,
            persona_id=row.persona_id,
            action=row.action,
            amount=row.amount,
        )
        for row in rows
    )


def _load_rating_transactions(
    session: Session,
    *,
    settlement: SettledMatch,
    lock: bool,
) -> tuple[MatchSettlementOriginalRatingTransaction, ...]:
    transaction_ids = settlement.rating_transaction_ids
    if not transaction_ids:
        return ()
    game_account_ids = tuple(
        sorted({item.game_account_id for item in settlement.ratings if item.rating_transaction_id is not None})
    )
    account_statement = (
        select(GameAccountORM.id).where(GameAccountORM.id.in_(game_account_ids)).order_by(GameAccountORM.id)
    )
    if lock:
        account_statement = account_statement.with_for_update()
    if tuple(session.scalars(account_statement)) != game_account_ids:
        raise MatchSettlementRollbackEvidenceExpiredError("operation evidence expired: a Rating GameAccount is missing")

    projection_statement = (
        select(RatingORM).where(RatingORM.game_account_id.in_(game_account_ids)).order_by(RatingORM.game_account_id)
    )
    if lock:
        projection_statement = projection_statement.with_for_update()
    projection_by_account = {row.game_account_id: row.rating for row in session.scalars(projection_statement)}

    transaction_statement = (
        select(RatingTransactionORM, MatchEntryORM.game_account_id)
        .join(MatchEntryORM, MatchEntryORM.id == RatingTransactionORM.match_entry_id)
        .where(RatingTransactionORM.id.in_(transaction_ids))
        .order_by(RatingTransactionORM.id)
    )
    if lock:
        transaction_statement = transaction_statement.with_for_update()
    rows = tuple(session.execute(transaction_statement))
    if tuple(row[0].id for row in rows) != transaction_ids:
        raise MatchSettlementRollbackEvidenceExpiredError(
            "operation evidence expired: a referenced Rating transaction is missing"
        )

    latest_rows = tuple(
        session.execute(
            select(
                MatchEntryORM.game_account_id,
                func.max(RatingTransactionORM.id).label("latest_transaction_id"),
            )
            .join(MatchEntryORM, MatchEntryORM.id == RatingTransactionORM.match_entry_id)
            .where(MatchEntryORM.game_account_id.in_(game_account_ids))
            .group_by(MatchEntryORM.game_account_id)
            .order_by(MatchEntryORM.game_account_id)
        )
    )
    latest_by_account = {
        row.game_account_id: _integer_value(row.latest_transaction_id, field_name="latest_rating_transaction_id")
        for row in latest_rows
    }
    return tuple(
        MatchSettlementOriginalRatingTransaction(
            transaction_id=transaction.id,
            operation_id=transaction.operation_id,
            rating_rule_version_id=transaction.rating_rule_version_id,
            match_entry_id=transaction.match_entry_id,
            game_account_id=game_account_id,
            rating_before=transaction.rating_before,
            amount=transaction.amount,
            rating_after=transaction.rating_after,
            current_rating=projection_by_account.get(game_account_id),
            latest_transaction_id=latest_by_account.get(game_account_id),
        )
        for transaction, game_account_id in rows
    )


def load_match_settlement_rollback_target(
    session: Session,
    *,
    match_id: int,
    lock: bool,
) -> MatchSettlementRollbackTarget | None:
    """Load one complete retained settlement bundle without ORM leakage."""

    match_statement = select(MatchORM).where(MatchORM.id == match_id)
    if lock:
        match_statement = match_statement.with_for_update()
    match = session.scalar(match_statement)
    if match is None:
        return None
    if (
        match.source_kind != MatchSourceKind.NATIVE_V2.value
        or match.status != MatchStatus.SETTLED.value
        or match.terminal_reason is not None
    ):
        return None

    settlement_operation_id, settlement = _load_original_settlement(
        session,
        match_id=match_id,
        lock=lock,
    )
    bets = _load_bets(
        session,
        match_id=match_id,
        expected_ids=settlement.active_bet_ids,
        lock=lock,
    )
    rating_transactions = _load_rating_transactions(
        session,
        settlement=settlement,
        lock=lock,
    )
    persona_ids = tuple(
        sorted(
            {item.persona_id for item in bets}
            | {item.persona_id for item in settlement.payouts}
            | {item.persona_id for item in settlement.rewards}
        )
    )
    wallets = _load_wallets(session, persona_ids=persona_ids, lock=lock)
    point_transactions = _load_point_transactions(
        session,
        transaction_ids=settlement.point_transaction_ids,
        lock=lock,
    )
    return MatchSettlementRollbackTarget(
        match_id=match.id,
        match_name=match.name,
        source_kind=MatchSourceKind(match.source_kind),
        status=MatchStatus(match.status),
        settlement_operation_id=settlement_operation_id,
        settlement=settlement,
        bets=bets,
        wallets=wallets,
        point_transactions=point_transactions,
        rating_transactions=rating_transactions,
    )


class SqlAlchemyMatchSettlementRollbackRepository:
    """Persist exact settlement compensation inside one Match-first transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._publications = SqlAlchemyMatchResultPublicationRepository(session)

    def lock_match(self, *, match_id: int) -> MatchSettlementRollbackLock | None:
        match = self._session.scalar(select(MatchORM).where(MatchORM.id == match_id).with_for_update())
        if match is None:
            return None
        return MatchSettlementRollbackLock(
            match_id=match.id,
            match_name=match.name,
            source_kind=MatchSourceKind(match.source_kind),
            status=MatchStatus(match.status),
        )

    def find_operation(self, *, idempotency_key: str) -> StoredMatchSettlementRollbackOperation | None:
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
        return StoredMatchSettlementRollbackOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            match_id=row.match_id,
            after_data=row.after_data,
        )

    def load_target(self, *, match_id: int, lock: bool) -> MatchSettlementRollbackTarget | None:
        return load_match_settlement_rollback_target(self._session, match_id=match_id, lock=lock)

    def persist_rollback(
        self,
        *,
        command: RollbackMatchSettlement,
        plan: MatchSettlementRollbackPlan,
        rolled_back_at: datetime,
    ) -> RolledBackMatchSettlement:
        stored_time = to_database_utc(rolled_back_at, field_name="rolled_back_at")
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
                MatchORM.id == plan.target.match_id,
                MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
                MatchORM.status == MatchStatus.SETTLED.value,
                MatchORM.terminal_reason.is_(None),
            )
            .values(
                status=MatchStatus.VOIDED.value,
                terminal_reason=command.reason,
                updated_at=stored_time,
            )
        )
        if changed_match.rowcount != 1:
            raise ValueError("Match changed before settlement rollback persistence.")

        cancelled_bet_ids = tuple(item.bet_id for item in plan.target.bets)
        if cancelled_bet_ids:
            changed_bets = self._session.execute(
                update(BetORM)
                .where(
                    BetORM.id.in_(cancelled_bet_ids),
                    BetORM.match_id == plan.target.match_id,
                    BetORM.status == BetStatus.SETTLED.value,
                    BetORM.active_marker.is_(None),
                )
                .values(status=BetStatus.CANCELLED.value, updated_at=stored_time)
            )
            if changed_bets.rowcount != len(cancelled_bet_ids):
                raise ValueError("Settled Bet set changed before rollback persistence.")

        payout_by_persona = {item.persona_id: item for item in plan.target.settlement.payouts}
        reward_by_persona = {item.persona_id: item for item in plan.target.settlement.rewards}
        bet_ids_by_persona: dict[str, list[int]] = {}
        for bet in plan.target.bets:
            bet_ids_by_persona.setdefault(bet.persona_id, []).append(bet.bet_id)

        point_compensations: list[MatchSettlementPointCompensation] = []
        for wallet in plan.wallets:
            changed_wallet = self._session.execute(
                update(CirclePointORM)
                .where(
                    CirclePointORM.persona_id == wallet.persona_id,
                    CirclePointORM.balance == wallet.balance_before,
                )
                .values(balance=wallet.balance_after, updated_at=stored_time)
            )
            if changed_wallet.rowcount != 1:
                raise ValueError("Locked Circle Point wallet changed before settlement compensation.")

            if wallet.payout_reversal:
                original = payout_by_persona[wallet.persona_id]
                row = PointTransactionORM(
                    persona_id=wallet.persona_id,
                    operation_id=operation.id,
                    action=MATCH_BET_PAYOUT_REVERSAL_POINT_ACTION,
                    amount=-wallet.payout_reversal,
                    created_at=stored_time,
                )
                self._session.add(row)
                self._session.flush()
                point_compensations.append(
                    MatchSettlementPointCompensation(
                        persona_id=wallet.persona_id,
                        action=row.action,
                        amount=row.amount,
                        point_transaction_id=row.id,
                        original_point_transaction_id=original.point_transaction_id,
                    )
                )
            if wallet.stake_refund:
                row = PointTransactionORM(
                    persona_id=wallet.persona_id,
                    operation_id=operation.id,
                    action=MATCH_BET_REFUND_POINT_ACTION,
                    amount=wallet.stake_refund,
                    created_at=stored_time,
                )
                self._session.add(row)
                self._session.flush()
                point_compensations.append(
                    MatchSettlementPointCompensation(
                        persona_id=wallet.persona_id,
                        action=row.action,
                        amount=row.amount,
                        point_transaction_id=row.id,
                        refunded_bet_ids=tuple(bet_ids_by_persona[wallet.persona_id]),
                    )
                )
            if wallet.reward_reversal:
                original = reward_by_persona[wallet.persona_id]
                row = PointTransactionORM(
                    persona_id=wallet.persona_id,
                    operation_id=operation.id,
                    action=MATCH_PLACEMENT_REWARD_REVERSAL_POINT_ACTION,
                    amount=-wallet.reward_reversal,
                    created_at=stored_time,
                )
                self._session.add(row)
                self._session.flush()
                point_compensations.append(
                    MatchSettlementPointCompensation(
                        persona_id=wallet.persona_id,
                        action=row.action,
                        amount=row.amount,
                        point_transaction_id=row.id,
                        original_point_transaction_id=original.point_transaction_id,
                    )
                )

        rating_compensations: list[MatchSettlementRatingCompensation] = []
        for rating in plan.ratings:
            projection = self._session.get(RatingORM, rating.game_account_id)
            if projection is None or projection.rating != rating.compensation_before:
                raise ValueError("Locked Rating projection changed before settlement compensation.")
            projection.rating = rating.compensation_after
            projection.updated_at = stored_time
            row = RatingTransactionORM(
                operation_id=operation.id,
                rating_rule_version_id=rating.rating_rule_version_id,
                match_entry_id=rating.match_entry_id,
                rating_before=rating.compensation_before,
                amount=rating.compensation_amount,
                rating_after=rating.compensation_after,
                created_at=stored_time,
            )
            self._session.add(row)
            self._session.flush()
            rating_compensations.append(
                MatchSettlementRatingCompensation(
                    original_rating_transaction_id=rating.original_transaction_id,
                    compensation_rating_transaction_id=row.id,
                    rating_rule_version_id=rating.rating_rule_version_id,
                    match_entry_id=rating.match_entry_id,
                    game_account_id=rating.game_account_id,
                    rating_before=row.rating_before,
                    amount=row.amount,
                    rating_after=row.rating_after,
                )
            )

        result = RolledBackMatchSettlement(
            match_id=plan.target.match_id,
            match_name=plan.target.match_name,
            previous_status=plan.target.status,
            status=MatchStatus.VOIDED,
            reason=command.reason,
            rolled_back_at=rolled_back_at,
            settlement_operation_id=plan.target.settlement_operation_id,
            settlement_fingerprint=plan.target.settlement.settlement_fingerprint,
            cancelled_bet_ids=cancelled_bet_ids,
            wallets=plan.wallets,
            point_compensations=tuple(point_compensations),
            rating_compensations=tuple(rating_compensations),
        )
        self._session.add(
            MatchOperationORM(
                operation_id=operation.id,
                match_id=plan.target.match_id,
                type=MatchSettlementRollbackAuditType.ROLLED_BACK.value,
                before_data={
                    "schema_version": MATCH_SETTLEMENT_ROLLBACK_AUDIT_SCHEMA_VERSION,
                    "match_id": plan.target.match_id,
                    "match_name": plan.target.match_name,
                    "status": plan.target.status.value,
                    "settlement_operation_id": plan.target.settlement_operation_id,
                    "settlement_fingerprint": plan.target.settlement.settlement_fingerprint,
                    "rollback_fingerprint": plan.rollback_fingerprint,
                    "active_bet_ids": list(cancelled_bet_ids),
                    "original_point_transaction_ids": list(plan.target.settlement.point_transaction_ids),
                    "original_rating_transaction_ids": list(plan.target.settlement.rating_transaction_ids),
                    "wallets": [item.to_payload() for item in plan.wallets],
                },
                after_data=result.to_audit_payload(),
            )
        )
        self._session.flush()
        return result

    def load_rollback_publication_source(
        self,
        *,
        match_id: int,
        guild_id: str,
    ) -> MatchSettlementVoidedPublicationSource | None:
        match = self._session.get(MatchORM, match_id)
        if (
            match is None
            or match.source_kind != MatchSourceKind.NATIVE_V2.value
            or match.status != MatchStatus.VOIDED.value
            or match.terminal_reason is None
        ):
            return None
        setting = self._session.get(BotGuildSettingORM, guild_id)
        return MatchSettlementVoidedPublicationSource(
            destination=MatchPublicationDestination(
                guild_id=guild_id,
                announcements_enabled=True if setting is None else setting.match_announcements_enabled,
                target_channel_id=None if setting is None else setting.match_announcement_channel_id,
            ),
            match_id=match.id,
            match_name=match.name,
            grade=MatchGrade(match.grade),
            scheduled_at=from_database_utc(match.scheduled_at, field_name="matches.scheduled_at"),
            rolled_back_at=from_database_utc(match.updated_at, field_name="matches.updated_at"),
            reason=match.terminal_reason,
        )

    def add_rollback_publication(
        self,
        *,
        intent: PublicationIntent,
        created_at: datetime,
    ) -> None:
        self._publications.add_publication(intent=intent, created_at=created_at)


class SqlAlchemyMatchSettlementRollbackQueryRepository:
    """Build bounded rollback choices and complete read-only compensation plans."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchSettlementRollbackTargetChoice, ...]:
        settlement_count = (
            select(func.count(MatchOperationORM.operation_id))
            .where(
                MatchOperationORM.match_id == MatchORM.id,
                MatchOperationORM.type == MatchSettlementAuditType.SETTLED.value,
            )
            .correlate(MatchORM)
            .scalar_subquery()
        )
        settled_at = (
            select(func.max(OperationORM.created_at))
            .join(MatchOperationORM, MatchOperationORM.operation_id == OperationORM.id)
            .where(
                MatchOperationORM.match_id == MatchORM.id,
                MatchOperationORM.type == MatchSettlementAuditType.SETTLED.value,
            )
            .correlate(MatchORM)
            .scalar_subquery()
        )
        settled_bet_count = (
            select(func.count(BetORM.id))
            .where(
                BetORM.match_id == MatchORM.id,
                BetORM.status == BetStatus.SETTLED.value,
            )
            .correlate(MatchORM)
            .scalar_subquery()
        )
        statement = select(
            MatchORM.id,
            MatchORM.name,
            settled_at.label("settled_at"),
            settled_bet_count.label("settled_bet_count"),
        ).where(
            MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
            MatchORM.status == MatchStatus.SETTLED.value,
            MatchORM.terminal_reason.is_(None),
            settlement_count == 1,
        )
        if search:
            statement = statement.where(MatchORM.name.contains(search))
        rows = tuple(self._session.execute(statement.order_by(MatchORM.scheduled_at, MatchORM.id).limit(limit)))
        return tuple(
            MatchSettlementRollbackTargetChoice(
                match_id=row.id,
                match_name=row.name,
                settled_at=from_database_utc(row.settled_at, field_name="settled_at"),
                settled_bet_count=_integer_value(row.settled_bet_count, field_name="settled_bet_count"),
            )
            for row in rows
        )

    def load_target(self, *, match_id: int) -> MatchSettlementRollbackTarget | None:
        return load_match_settlement_rollback_target(self._session, match_id=match_id, lock=False)


class SqlAlchemyMatchSettlementRollbackUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete command UoW exposing terminal settlement rollback."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._repository: SqlAlchemyMatchSettlementRollbackRepository | None = None

    @property
    def match_settlement_rollback(self) -> SqlAlchemyMatchSettlementRollbackRepository:
        return self._require_active_repository(self._repository)

    def _activate_repositories(self) -> None:
        self._repository = SqlAlchemyMatchSettlementRollbackRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._repository = None


class SqlAlchemyMatchSettlementRollbackUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchSettlementRollbackUnitOfWork]
):
    """Create one fresh terminal rollback command UoW."""

    unit_of_work_type = SqlAlchemyMatchSettlementRollbackUnitOfWork


class SqlAlchemyMatchSettlementRollbackQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW exposing terminal rollback projections."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._repository: SqlAlchemyMatchSettlementRollbackQueryRepository | None = None

    @property
    def match_settlement_rollback_queries(self) -> SqlAlchemyMatchSettlementRollbackQueryRepository:
        return self._require_active_repository(self._repository)

    def _activate_repositories(self) -> None:
        self._repository = SqlAlchemyMatchSettlementRollbackQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._repository = None


class SqlAlchemyMatchSettlementRollbackQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchSettlementRollbackQueryUnitOfWork]
):
    """Create one fresh terminal rollback query UoW."""

    unit_of_work_type = SqlAlchemyMatchSettlementRollbackQueryUnitOfWork
