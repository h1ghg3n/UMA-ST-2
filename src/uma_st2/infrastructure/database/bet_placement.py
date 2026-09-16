"""SQLAlchemy implementation of native member Bet placement."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from uma_st2.application.betting import (
    BET_PLACEMENT_AUDIT_SCHEMA_VERSION,
    BET_STAKE_POINT_ACTION,
    BetPlacementAuditType,
    BetPlacementEntry,
    BetPlacementPersona,
    BetPlacementTarget,
    BetPlacementWallet,
    PlacedMatchBet,
    PlaceMatchBet,
    StoredBetPlacementOperation,
)
from uma_st2.domain.betting import BetStatus, BetType
from uma_st2.domain.identity import PersonaStatus
from uma_st2.domain.match import MatchSourceKind, MatchStatus

from .datetime_codec import to_database_utc
from .orm import (
    BetOperationORM,
    BetORM,
    CirclePointORM,
    DiscordAccountORM,
    GameAccountORM,
    MatchEntryORM,
    MatchORM,
    OperationORM,
    PersonaORM,
    PointTransactionORM,
)
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


class SqlAlchemyBetPlacementRepository:
    """Lock placement authority and persist one atomic stake mutation."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_target(self, *, match_id: int) -> BetPlacementTarget | None:
        match = self._session.scalar(select(MatchORM).where(MatchORM.id == match_id).with_for_update())
        if match is None:
            return None
        entry_rows = self._session.execute(
            select(MatchEntryORM.id, MatchEntryORM.entry_number)
            .where(MatchEntryORM.match_id == match_id)
            .order_by(MatchEntryORM.entry_number, MatchEntryORM.id)
            .with_for_update()
        )
        return BetPlacementTarget(
            match_id=match.id,
            match_name=match.name,
            source_kind=MatchSourceKind(match.source_kind),
            status=MatchStatus(match.status),
            entries=tuple(BetPlacementEntry(id=row.id, entry_number=row.entry_number) for row in entry_rows),
        )

    def find_operation(self, *, idempotency_key: str) -> StoredBetPlacementOperation | None:
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
        return StoredBetPlacementOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            match_id=row.match_id,
            bet_id=row.bet_id,
            after_data=row.after_data,
        )

    def lock_actor_persona(self, *, discord_user_id: str) -> BetPlacementPersona | None:
        discord_account = self._session.scalar(
            select(DiscordAccountORM).where(DiscordAccountORM.discord_user_id == discord_user_id).with_for_update()
        )
        if discord_account is None or discord_account.persona_id is None:
            return None
        persona = self._session.scalar(
            select(PersonaORM).where(PersonaORM.id == discord_account.persona_id).with_for_update()
        )
        if persona is None:
            raise ValueError("DiscordAccount references a missing Persona.")
        qualifying_account_id = self._session.scalar(
            select(GameAccountORM.id)
            .where(
                GameAccountORM.persona_id == persona.id,
                GameAccountORM.uma_pid.is_not(None),
            )
            .order_by(GameAccountORM.id)
            .limit(1)
            .with_for_update()
        )
        return BetPlacementPersona(
            id=persona.id,
            status=PersonaStatus(persona.status),
            has_eligible_game_account=qualifying_account_id is not None,
        )

    def lock_wallet(self, *, persona_id: str) -> BetPlacementWallet | None:
        wallet = self._session.scalar(
            select(CirclePointORM).where(CirclePointORM.persona_id == persona_id).with_for_update()
        )
        if wallet is None:
            return None
        return BetPlacementWallet(persona_id=wallet.persona_id, balance=wallet.balance)

    def find_active_duplicate(
        self,
        *,
        match_id: int,
        persona_id: str,
        bet_type: BetType,
        selection_fingerprint: str,
    ) -> int | None:
        inconsistent = self._session.scalar(
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
                )
                .order_by(BetORM.id)
                .limit(2)
            )
        )
        if len(duplicate_ids) > 1:
            raise ValueError("Multiple active duplicate Bets exist.")
        return duplicate_ids[0] if duplicate_ids else None

    def persist_placement(
        self,
        *,
        command: PlaceMatchBet,
        target: BetPlacementTarget,
        persona: BetPlacementPersona,
        wallet: BetPlacementWallet,
        selections: tuple[BetPlacementEntry, ...],
        selection_fingerprint: str,
        balance_after: int,
        placed_at: datetime,
    ) -> PlacedMatchBet:
        stored_time = to_database_utc(placed_at, field_name="placed_at")
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

        bet = BetORM(
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
        self._session.add(bet)
        self._session.flush()

        changed = self._session.execute(
            update(CirclePointORM)
            .where(
                CirclePointORM.persona_id == persona.id,
                CirclePointORM.balance == wallet.balance,
            )
            .values(balance=balance_after, updated_at=stored_time)
        )
        if changed.rowcount != 1:
            raise ValueError("Locked Circle Point wallet changed before debit.")
        self._session.add(
            PointTransactionORM(
                persona_id=persona.id,
                operation_id=operation.id,
                action=BET_STAKE_POINT_ACTION,
                amount=-command.amount,
                created_at=stored_time,
            )
        )
        placed = PlacedMatchBet(
            bet_id=bet.id,
            match_id=target.match_id,
            match_name=target.match_name,
            persona_id=persona.id,
            bet_type=command.bet_type,
            selections=selections,
            selection_fingerprint=selection_fingerprint,
            amount=command.amount,
            status=BetStatus.ACTIVE,
            balance_after=balance_after,
            placed_at=placed_at,
        )
        self._session.add(
            BetOperationORM(
                operation_id=operation.id,
                match_id=target.match_id,
                bet_id=bet.id,
                type=BetPlacementAuditType.PLACED.value,
                before_data={
                    "schema_version": BET_PLACEMENT_AUDIT_SCHEMA_VERSION,
                    "match_id": target.match_id,
                    "match_name": target.match_name,
                    "persona_id": persona.id,
                    "balance_before": wallet.balance,
                },
                after_data=placed.to_audit_payload(),
            )
        )
        self._session.flush()
        return placed


class SqlAlchemyBetPlacementUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete UoW for one native member Bet placement."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._bet_placement: SqlAlchemyBetPlacementRepository | None = None

    @property
    def bet_placement(self) -> SqlAlchemyBetPlacementRepository:
        return self._require_active_repository(self._bet_placement)

    def _activate_repositories(self) -> None:
        self._bet_placement = SqlAlchemyBetPlacementRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._bet_placement = None


class SqlAlchemyBetPlacementUnitOfWorkFactory(SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyBetPlacementUnitOfWork]):
    """Create one fresh placement UoW per command."""

    unit_of_work_type = SqlAlchemyBetPlacementUnitOfWork
