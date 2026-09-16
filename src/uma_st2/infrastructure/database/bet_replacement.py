"""SQLAlchemy implementation of native member Bet replacement."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from uma_st2.application.betting import (
    BET_REPLACEMENT_AUDIT_SCHEMA_VERSION,
    BET_REPLACEMENT_REFUND_POINT_ACTION,
    BET_STAKE_POINT_ACTION,
    BetPlacementEntry,
    BetPlacementPersona,
    BetPlacementTarget,
    BetPlacementWallet,
    BetReplacementAuditType,
    BetReplacementBet,
    PlacedMatchBet,
    ReplacedMatchBet,
    ReplaceMatchBet,
    StoredBetReplacementOperation,
)
from uma_st2.domain.betting import BetStatus, BetType, canonicalize_selections

from .bet_placement import SqlAlchemyBetPlacementRepository
from .datetime_codec import to_database_utc
from .orm import BetOperationORM, BetORM, CirclePointORM, MatchEntryORM, OperationORM, PointTransactionORM
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


def _validate_bet_markers(session: Session, *, match_id: int) -> None:
    inconsistent = session.scalar(
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
    if inconsistent is not None:
        raise ValueError("Bet status and active marker are inconsistent.")


class SqlAlchemyBetReplacementRepository:
    """Lock replacement authority and persist one atomic immutable Bet swap."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._placement = SqlAlchemyBetPlacementRepository(session)

    def resolve_candidate_match_id(self, *, bet_id: int) -> int | None:
        return self._session.scalar(select(BetORM.match_id).where(BetORM.id == bet_id))

    def lock_target(self, *, match_id: int) -> BetPlacementTarget | None:
        return self._placement.lock_target(match_id=match_id)

    def find_operation(self, *, idempotency_key: str) -> StoredBetReplacementOperation | None:
        row = self._session.execute(
            select(
                OperationORM.request_fingerprint,
                BetOperationORM.type,
                BetOperationORM.match_id,
                BetOperationORM.bet_id,
                BetOperationORM.after_data,
            )
            .outerjoin(BetOperationORM, BetOperationORM.operation_id == OperationORM.id)
            .where(OperationORM.idempotency_key == idempotency_key)
            .with_for_update()
        ).one_or_none()
        if row is None:
            return None
        return StoredBetReplacementOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            match_id=row.match_id,
            bet_id=row.bet_id,
            after_data=row.after_data,
        )

    def lock_bet(self, *, bet_id: int, match_id: int) -> BetReplacementBet | None:
        _validate_bet_markers(self._session, match_id=match_id)
        bet = self._session.scalar(
            select(BetORM)
            .where(
                BetORM.id == bet_id,
                BetORM.match_id == match_id,
            )
            .with_for_update()
        )
        if bet is None:
            return None
        if not isinstance(bet.selections, list):
            raise ValueError("Bet selections must be stored as a JSON array.")
        bet_type = BetType(bet.type)
        raw_selection_ids = tuple(bet.selections)
        selection_ids = canonicalize_selections(bet_type, raw_selection_ids)
        if raw_selection_ids != selection_ids:
            raise ValueError("Bet selections must use canonical Entry ID order.")
        entry_rows = tuple(
            self._session.execute(
                select(MatchEntryORM.id, MatchEntryORM.entry_number)
                .where(
                    MatchEntryORM.match_id == match_id,
                    MatchEntryORM.id.in_(selection_ids),
                )
                .order_by(MatchEntryORM.id)
                .with_for_update()
            )
        )
        entries_by_id = {row.id: BetPlacementEntry(id=row.id, entry_number=row.entry_number) for row in entry_rows}
        try:
            selections = tuple(entries_by_id[entry_id] for entry_id in selection_ids)
        except (KeyError, TypeError) as exc:
            raise ValueError("Bet references a missing or malformed Match Entry.") from exc
        return BetReplacementBet(
            id=bet.id,
            match_id=bet.match_id,
            persona_id=bet.persona_id,
            bet_type=bet_type,
            selections=selections,
            selection_fingerprint=bet.selection_fingerprint,
            amount=bet.amount,
            status=BetStatus(bet.status),
        )

    def lock_actor_persona(self, *, discord_user_id: str) -> BetPlacementPersona | None:
        return self._placement.lock_actor_persona(discord_user_id=discord_user_id)

    def lock_wallet(self, *, persona_id: str) -> BetPlacementWallet | None:
        return self._placement.lock_wallet(persona_id=persona_id)

    def find_active_duplicate(
        self,
        *,
        match_id: int,
        persona_id: str,
        bet_type: BetType,
        selection_fingerprint: str,
        excluding_bet_id: int,
    ) -> int | None:
        _validate_bet_markers(self._session, match_id=match_id)
        duplicate_ids = tuple(
            self._session.scalars(
                select(BetORM.id)
                .where(
                    BetORM.match_id == match_id,
                    BetORM.persona_id == persona_id,
                    BetORM.type == BetType(bet_type).value,
                    BetORM.selection_fingerprint == selection_fingerprint,
                    BetORM.status == BetStatus.ACTIVE.value,
                    BetORM.active_marker.is_(True),
                    BetORM.id != excluding_bet_id,
                )
                .order_by(BetORM.id)
                .limit(2)
            )
        )
        if len(duplicate_ids) > 1:
            raise ValueError("Multiple active duplicate Bets exist.")
        return duplicate_ids[0] if duplicate_ids else None

    def persist_replacement(
        self,
        *,
        command: ReplaceMatchBet,
        target: BetPlacementTarget,
        persona: BetPlacementPersona,
        wallet: BetPlacementWallet,
        old_bet: BetReplacementBet,
        selections: tuple[BetPlacementEntry, ...],
        selection_fingerprint: str,
        balance_after_refund: int,
        balance_after: int,
        replaced_at: datetime,
    ) -> ReplacedMatchBet:
        stored_time = to_database_utc(replaced_at, field_name="replaced_at")
        operation = OperationORM(
            guild_id=command.guild_id,
            correlation_id=command.correlation_id,
            actor_discord_user_id=command.actor_discord_user_id,
            idempotency_key=command.idempotency_key,
            request_fingerprint=command.request_fingerprint,
            reason=None,
            created_at=stored_time,
        )
        self._session.add(operation)
        self._session.flush()

        changed_old = self._session.execute(
            update(BetORM)
            .where(
                BetORM.id == old_bet.id,
                BetORM.match_id == target.match_id,
                BetORM.persona_id == persona.id,
                BetORM.status == BetStatus.ACTIVE.value,
                BetORM.active_marker.is_(True),
            )
            .values(
                status=BetStatus.CANCELLED.value,
                active_marker=None,
                updated_at=stored_time,
            )
        )
        if changed_old.rowcount != 1:
            raise ValueError("Selected active Bet changed before replacement.")

        new_bet = BetORM(
            match_id=target.match_id,
            persona_id=persona.id,
            type=command.bet_type.value,
            selections=[selection.id for selection in selections],
            selection_fingerprint=selection_fingerprint,
            amount=command.amount,
            status=BetStatus.ACTIVE.value,
            active_marker=True,
            created_at=stored_time,
            updated_at=stored_time,
        )
        self._session.add(new_bet)
        self._session.flush()

        changed_wallet = self._session.execute(
            update(CirclePointORM)
            .where(
                CirclePointORM.persona_id == persona.id,
                CirclePointORM.balance == wallet.balance,
            )
            .values(balance=balance_after, updated_at=stored_time)
        )
        if changed_wallet.rowcount != 1:
            raise ValueError("Locked Circle Point wallet changed before replacement.")

        refund_transaction = PointTransactionORM(
            persona_id=persona.id,
            operation_id=operation.id,
            action=BET_REPLACEMENT_REFUND_POINT_ACTION,
            amount=old_bet.amount,
            created_at=stored_time,
        )
        stake_transaction = PointTransactionORM(
            persona_id=persona.id,
            operation_id=operation.id,
            action=BET_STAKE_POINT_ACTION,
            amount=-command.amount,
            created_at=stored_time,
        )
        self._session.add_all((refund_transaction, stake_transaction))
        self._session.flush()

        placed = PlacedMatchBet(
            bet_id=new_bet.id,
            match_id=target.match_id,
            match_name=target.match_name,
            persona_id=persona.id,
            bet_type=command.bet_type,
            selections=selections,
            selection_fingerprint=selection_fingerprint,
            amount=command.amount,
            status=BetStatus.ACTIVE,
            balance_after=balance_after,
            placed_at=replaced_at,
        )
        replaced = ReplacedMatchBet(
            old_bet=old_bet.with_status(BetStatus.CANCELLED),
            new_bet=placed,
            balance_before=wallet.balance,
            balance_after_refund=balance_after_refund,
            balance_after=balance_after,
            refund_point_transaction_id=refund_transaction.id,
            stake_point_transaction_id=stake_transaction.id,
            replaced_at=replaced_at,
        )
        self._session.add(
            BetOperationORM(
                operation_id=operation.id,
                match_id=target.match_id,
                bet_id=new_bet.id,
                type=BetReplacementAuditType.REPLACED.value,
                before_data={
                    "schema_version": BET_REPLACEMENT_AUDIT_SCHEMA_VERSION,
                    "old_bet": old_bet.to_audit_payload(),
                    "balance_before": wallet.balance,
                },
                after_data=replaced.to_audit_payload(),
            )
        )
        self._session.flush()
        return replaced


class SqlAlchemyBetReplacementUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete UoW for one native member Bet replacement."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._bet_replacement: SqlAlchemyBetReplacementRepository | None = None

    @property
    def bet_replacement(self) -> SqlAlchemyBetReplacementRepository:
        return self._require_active_repository(self._bet_replacement)

    def _activate_repositories(self) -> None:
        self._bet_replacement = SqlAlchemyBetReplacementRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._bet_replacement = None


class SqlAlchemyBetReplacementUnitOfWorkFactory(SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyBetReplacementUnitOfWork]):
    """Create one fresh replacement UoW per command."""

    unit_of_work_type = SqlAlchemyBetReplacementUnitOfWork
