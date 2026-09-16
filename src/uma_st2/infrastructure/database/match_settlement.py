"""SQLAlchemy implementation of atomic native V2 Match settlement."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from uma_st2.application.match import (
    MATCH_BET_PAYOUT_POINT_ACTION,
    MATCH_PLACEMENT_REWARD_POINT_ACTION,
    MatchSettlementAuditType,
    MatchSettlementBet,
    MatchSettlementEntry,
    MatchSettlementLock,
    MatchSettlementPayout,
    MatchSettlementPlan,
    MatchSettlementRating,
    MatchSettlementResultAuthority,
    MatchSettlementReward,
    MatchSettlementRuleReference,
    MatchSettlementRuleUnavailableError,
    MatchSettlementTarget,
    MatchSettlementTargetChoice,
    MatchSettlementWallet,
    MatchSettlementWalletUnavailableError,
    SettledMatch,
    SettleMatch,
    StoredMatchSettlementOperation,
)
from uma_st2.application.publication import MatchResultPublicationSource, PublicationIntent
from uma_st2.domain.betting import BetStatus, BetType
from uma_st2.domain.match import MatchGrade, MatchSourceKind, MatchStatus
from uma_st2.domain.rating import RatingRule

from .datetime_codec import from_database_utc, to_database_utc
from .match_publication import SqlAlchemyMatchResultPublicationRepository
from .match_result_submission_projection import load_match_result_submission_target
from .orm import (
    BetORM,
    CirclePointORM,
    GameAccountORM,
    MatchEntryORM,
    MatchOperationORM,
    MatchORM,
    MatchResultSubmissionORM,
    OperationORM,
    PointTransactionORM,
    RatingORM,
    RatingRuleORM,
    RatingRuleVersionORM,
    RatingTransactionORM,
    UmamusumeORM,
    UmamusumeVariantORM,
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


def _lock_settlement_owned_rows(session: Session, *, match_id: int) -> None:
    """Lock Match-owned rows without joined shared-master lock expansion."""

    tuple(
        session.scalars(
            select(MatchEntryORM.id)
            .where(MatchEntryORM.match_id == match_id)
            .order_by(MatchEntryORM.id)
            .with_for_update()
        )
    )
    tuple(
        session.scalars(
            select(MatchResultSubmissionORM.id)
            .where(MatchResultSubmissionORM.match_id == match_id)
            .order_by(MatchResultSubmissionORM.id)
            .with_for_update()
        )
    )


def _load_active_bets(session: Session, *, match_id: int, lock: bool) -> tuple[MatchSettlementBet, ...]:
    statement = select(BetORM).where(BetORM.match_id == match_id).order_by(BetORM.id)
    if lock:
        statement = statement.with_for_update()
    bet_rows = tuple(session.scalars(statement))
    for bet in bet_rows:
        BetStatus(bet.status)
    if any((bet.status == BetStatus.ACTIVE.value) != (bet.active_marker is True) for bet in bet_rows):
        raise ValueError("Bet status and active marker are inconsistent.")
    if any(bet.status == BetStatus.SETTLED.value for bet in bet_rows):
        raise ValueError("A result-confirmed Match cannot contain a settled Bet.")
    return tuple(
        MatchSettlementBet(
            bet_id=bet.id,
            persona_id=bet.persona_id,
            bet_type=BetType(bet.type),
            selection_entry_ids=tuple(bet.selections),
            amount=bet.amount,
        )
        for bet in bet_rows
        if bet.status == BetStatus.ACTIVE.value and bet.active_marker is True
    )


def _load_game_accounts(
    session: Session,
    *,
    game_account_ids: tuple[int, ...],
    lock: bool,
) -> dict[int, GameAccountORM]:
    statement = select(GameAccountORM).where(GameAccountORM.id.in_(game_account_ids)).order_by(GameAccountORM.id)
    if lock:
        statement = statement.with_for_update()
    rows = tuple(session.scalars(statement))
    if tuple(row.id for row in rows) != game_account_ids:
        raise ValueError("Match Entry references a missing GameAccount.")
    return {row.id: row for row in rows}


def _load_horse_names(session: Session, *, entries: tuple[MatchEntryORM, ...]) -> dict[int, str]:
    base_ids = tuple(sorted({entry.umamusume_id for entry in entries}))
    base_rows = tuple(
        session.execute(
            select(UmamusumeORM.id, UmamusumeORM.name_ko, UmamusumeORM.name_jp)
            .where(UmamusumeORM.id.in_(base_ids))
            .order_by(UmamusumeORM.id)
        )
    )
    if tuple(row.id for row in base_rows) != base_ids:
        raise ValueError("Match Entry references a missing Umamusume.")
    base_names = {row.id: row.name_ko or row.name_jp for row in base_rows}

    variant_ids = tuple(
        sorted({entry.umamusume_variant_id for entry in entries if entry.umamusume_variant_id is not None})
    )
    variant_names: dict[int, str] = {}
    if variant_ids:
        variant_rows = tuple(
            session.execute(
                select(
                    UmamusumeVariantORM.id,
                    UmamusumeVariantORM.umamusume_id,
                    UmamusumeVariantORM.name_ko,
                    UmamusumeVariantORM.name_jp,
                )
                .where(UmamusumeVariantORM.id.in_(variant_ids))
                .order_by(UmamusumeVariantORM.id)
            )
        )
        if tuple(row.id for row in variant_rows) != variant_ids:
            raise ValueError("Match Entry references a missing UmamusumeVariant.")
        variant_by_id = {row.id: row for row in variant_rows}
        for entry in entries:
            if entry.umamusume_variant_id is None:
                continue
            variant = variant_by_id[entry.umamusume_variant_id]
            if variant.umamusume_id != entry.umamusume_id:
                raise ValueError("Match Entry UmamusumeVariant ownership is inconsistent.")
            variant_names[variant.id] = variant.name_ko or variant.name_jp
    return {
        entry.id: (
            variant_names[entry.umamusume_variant_id]
            if entry.umamusume_variant_id is not None
            else base_names[entry.umamusume_id]
        )
        for entry in entries
    }


def _load_ratings(
    session: Session,
    *,
    game_account_ids: tuple[int, ...],
    lock: bool,
) -> dict[int, Decimal]:
    statement = (
        select(RatingORM).where(RatingORM.game_account_id.in_(game_account_ids)).order_by(RatingORM.game_account_id)
    )
    if lock:
        statement = statement.with_for_update()
    return {row.game_account_id: row.rating for row in session.scalars(statement)}


def _load_rating_authority(
    session: Session,
    *,
    grade: MatchGrade,
    lock: bool,
) -> tuple[MatchSettlementRuleReference | None, tuple[RatingRule, ...]]:
    if grade is MatchGrade.OP:
        return None, ()
    statement = (
        select(RatingRuleVersionORM)
        .order_by(
            RatingRuleVersionORM.version_number.desc(),
            RatingRuleVersionORM.id.desc(),
        )
        .limit(1)
    )
    if lock:
        statement = statement.with_for_update()
    version = session.scalar(statement)
    if version is None:
        raise MatchSettlementRuleUnavailableError("No reviewed Rating rule version is available.")
    actual_rule_count = session.scalar(
        select(func.count(RatingRuleORM.id)).where(RatingRuleORM.rating_rule_version_id == version.id)
    )
    if _integer_value(actual_rule_count, field_name="rating_rule_count") != version.rule_count:
        raise MatchSettlementRuleUnavailableError("Current Rating rule version is incomplete.")
    rules: tuple[RatingRule, ...] = ()
    if grade in (MatchGrade.G1, MatchGrade.G2, MatchGrade.G3):
        rows = tuple(
            session.scalars(
                select(RatingRuleORM)
                .where(
                    RatingRuleORM.rating_rule_version_id == version.id,
                    RatingRuleORM.grade == grade.value,
                )
                .order_by(
                    RatingRuleORM.participant_count,
                    RatingRuleORM.converted_rank,
                    RatingRuleORM.id,
                )
            )
        )
        rules = tuple(
            RatingRule(
                grade=MatchGrade(row.grade),
                participant_count=row.participant_count,
                converted_rank=row.converted_rank,
                base_delta=row.base_delta,
            )
            for row in rows
        )
    return (
        MatchSettlementRuleReference(
            version_id=version.id,
            version_number=version.version_number,
            rule_set_checksum=version.rule_set_checksum,
        ),
        rules,
    )


def _load_wallets(
    session: Session,
    *,
    persona_ids: tuple[str, ...],
    lock: bool,
) -> tuple[MatchSettlementWallet, ...]:
    statement = (
        select(CirclePointORM).where(CirclePointORM.persona_id.in_(persona_ids)).order_by(CirclePointORM.persona_id)
    )
    if lock:
        statement = statement.with_for_update()
    rows = tuple(session.scalars(statement))
    if tuple(row.persona_id for row in rows) != persona_ids:
        raise MatchSettlementWalletUnavailableError(
            "Every active-Bet or placement-reward Persona must retain a canonical wallet."
        )
    return tuple(MatchSettlementWallet(row.persona_id, row.balance) for row in rows)


def load_match_settlement_target(
    session: Session,
    *,
    match_id: int,
    lock: bool,
) -> MatchSettlementTarget | None:
    """Build one complete settlement authority without leaking ORM rows."""

    if lock:
        _lock_settlement_owned_rows(session, match_id=match_id)
    result_target = load_match_result_submission_target(session, match_id=match_id, lock=False)
    if result_target is None:
        return None
    if result_target.pending is not None:
        raise ValueError("Settlement cannot consume a Match with a pending ResultSubmission.")
    if result_target.confirmed is None:
        raise ValueError("Settlement requires one current confirmed ResultSubmission.")

    match_row = session.execute(
        select(
            MatchORM.id,
            MatchORM.name,
            MatchORM.source_kind,
            MatchORM.status,
            MatchORM.grade,
            MatchORM.scheduled_at,
        ).where(MatchORM.id == match_id)
    ).one()

    entry_statement = select(MatchEntryORM).where(MatchEntryORM.match_id == match_id).order_by(MatchEntryORM.id)
    entries = tuple(session.scalars(entry_statement))
    if any(entry.rating_disposition is not None for entry in entries):
        raise ValueError("A result-confirmed Match cannot already have a Rating disposition.")
    game_account_ids = tuple(sorted({entry.game_account_id for entry in entries}))
    accounts = _load_game_accounts(session, game_account_ids=game_account_ids, lock=lock)
    horse_names = _load_horse_names(session, entries=entries)
    ratings = _load_ratings(session, game_account_ids=game_account_ids, lock=lock)
    grade = MatchGrade(match_row.grade)
    rule_reference, rating_rules = _load_rating_authority(
        session,
        grade=grade,
        lock=lock,
    )
    active_bets = _load_active_bets(session, match_id=match_id, lock=lock)
    required_personas = {bet.persona_id for bet in active_bets}
    if grade is not MatchGrade.OP:
        required_personas.update(entry.owner_at_event_persona_id for entry in entries)
    wallets = _load_wallets(
        session,
        persona_ids=tuple(sorted(required_personas)),
        lock=lock,
    )
    return MatchSettlementTarget(
        match_id=match_row.id,
        match_name=match_row.name,
        source_kind=MatchSourceKind(match_row.source_kind),
        status=MatchStatus(match_row.status),
        grade=grade,
        scheduled_at=from_database_utc(match_row.scheduled_at, field_name="matches.scheduled_at"),
        result=MatchSettlementResultAuthority(
            submission_id=result_target.confirmed.submission_id,
            revision_number=result_target.confirmed.revision_number,
            candidate_fingerprint=result_target.confirmed.candidate.fingerprint,
        ),
        entries=tuple(
            MatchSettlementEntry(
                match_entry_id=entry.id,
                entry_number=entry.entry_number,
                game_account_id=entry.game_account_id,
                owner_at_event_persona_id=entry.owner_at_event_persona_id,
                game_account_name=accounts[entry.game_account_id].nickname,
                horse_name=horse_names[entry.id],
                affiliation_at_event=entry.affiliation_at_event,
                rank=entry.rank,
                rating_before=ratings.get(entry.game_account_id, Decimal()),
            )
            for entry in entries
        ),
        active_bets=active_bets,
        wallets=wallets,
        rating_rule_version=rule_reference,
        rating_rules=rating_rules,
    )


class SqlAlchemyMatchSettlementRepository:
    """Revalidate and persist one settlement inside a Match-first transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._result_publications = SqlAlchemyMatchResultPublicationRepository(session)

    def lock_match(self, *, match_id: int) -> MatchSettlementLock | None:
        match = self._session.scalar(select(MatchORM).where(MatchORM.id == match_id).with_for_update())
        if match is None:
            return None
        return MatchSettlementLock(
            match_id=match.id,
            match_name=match.name,
            source_kind=MatchSourceKind(match.source_kind),
            status=MatchStatus(match.status),
        )

    def find_operation(self, *, idempotency_key: str) -> StoredMatchSettlementOperation | None:
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
        return StoredMatchSettlementOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            match_id=row.match_id,
            after_data=row.after_data,
        )

    def load_target(self, *, match_id: int, lock: bool) -> MatchSettlementTarget | None:
        return load_match_settlement_target(self._session, match_id=match_id, lock=lock)

    def persist_settlement(
        self,
        *,
        command: SettleMatch,
        plan: MatchSettlementPlan,
        settled_at: datetime,
    ) -> SettledMatch:
        stored_time = to_database_utc(settled_at, field_name="settled_at")
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
                MatchORM.status == MatchStatus.RESULT_CONFIRMED.value,
                MatchORM.terminal_reason.is_(None),
            )
            .values(status=MatchStatus.SETTLED.value, updated_at=stored_time)
        )
        if changed_match.rowcount != 1:
            raise ValueError("Match changed before settlement persistence.")

        active_bet_ids = tuple(bet.bet_id for bet in plan.target.active_bets)
        if active_bet_ids:
            changed_bets = self._session.execute(
                update(BetORM)
                .where(
                    BetORM.id.in_(active_bet_ids),
                    BetORM.match_id == plan.target.match_id,
                    BetORM.status == BetStatus.ACTIVE.value,
                    BetORM.active_marker.is_(True),
                )
                .values(
                    status=BetStatus.SETTLED.value,
                    active_marker=None,
                    updated_at=stored_time,
                )
            )
            if changed_bets.rowcount != len(active_bet_ids):
                raise ValueError("Active Bet set changed before settlement persistence.")

        payout_transaction_ids: dict[str, int] = {}
        reward_transaction_ids: dict[str, int] = {}
        payout_by_persona = {payout.persona_id: payout for payout in plan.payouts}
        reward_by_persona = {reward.persona_id: reward for reward in plan.rewards}
        for wallet_delta in plan.wallet_deltas:
            changed_wallet = self._session.execute(
                update(CirclePointORM)
                .where(
                    CirclePointORM.persona_id == wallet_delta.persona_id,
                    CirclePointORM.balance == wallet_delta.balance_before,
                )
                .values(balance=wallet_delta.balance_after, updated_at=stored_time)
            )
            if changed_wallet.rowcount != 1:
                raise ValueError("Locked Circle Point wallet changed before settlement credit.")
            payout = payout_by_persona.get(wallet_delta.persona_id)
            if payout is not None:
                point_transaction = PointTransactionORM(
                    persona_id=payout.persona_id,
                    operation_id=operation.id,
                    action=MATCH_BET_PAYOUT_POINT_ACTION,
                    amount=payout.amount,
                    created_at=stored_time,
                )
                self._session.add(point_transaction)
                self._session.flush()
                payout_transaction_ids[payout.persona_id] = point_transaction.id
            reward = reward_by_persona.get(wallet_delta.persona_id)
            if reward is not None:
                point_transaction = PointTransactionORM(
                    persona_id=reward.persona_id,
                    operation_id=operation.id,
                    action=MATCH_PLACEMENT_REWARD_POINT_ACTION,
                    amount=reward.amount,
                    created_at=stored_time,
                )
                self._session.add(point_transaction)
                self._session.flush()
                reward_transaction_ids[reward.persona_id] = point_transaction.id

        rating_transaction_ids: dict[int, int] = {}
        disposition_by_entry_id = {rating.match_entry_id: rating.rating_disposition.value for rating in plan.ratings}
        for entry_id, disposition in disposition_by_entry_id.items():
            changed = self._session.execute(
                update(MatchEntryORM)
                .where(
                    MatchEntryORM.id == entry_id,
                    MatchEntryORM.match_id == plan.target.match_id,
                    MatchEntryORM.rating_disposition.is_(None),
                )
                .values(rating_disposition=disposition, updated_at=stored_time)
            )
            if changed.rowcount != 1:
                raise ValueError("Match Entry Rating disposition changed before settlement persistence.")
        for rating in plan.ratings:
            if not rating.transaction_required:
                continue
            if plan.target.rating_rule_version is None:
                raise ValueError("Rating transaction requires an immutable rule version.")
            projection = self._session.get(RatingORM, rating.game_account_id)
            if projection is None:
                if rating.rating_before != 0:
                    raise ValueError("Missing Rating projection has a non-zero locked value.")
                projection = RatingORM(
                    game_account_id=rating.game_account_id,
                    rating=rating.rating_after,
                    updated_at=stored_time,
                )
                self._session.add(projection)
            else:
                if projection.rating != rating.rating_before:
                    raise ValueError("Locked Rating projection changed before settlement.")
                projection.rating = rating.rating_after
                projection.updated_at = stored_time
            transaction = RatingTransactionORM(
                operation_id=operation.id,
                rating_rule_version_id=plan.target.rating_rule_version.version_id,
                match_entry_id=rating.match_entry_id,
                rating_before=rating.rating_before,
                amount=rating.amount,
                rating_after=rating.rating_after,
                created_at=stored_time,
            )
            self._session.add(transaction)
            self._session.flush()
            rating_transaction_ids[rating.match_entry_id] = transaction.id

        settled = SettledMatch(
            match_id=plan.target.match_id,
            match_name=plan.target.match_name,
            previous_status=plan.target.status,
            status=MatchStatus.SETTLED,
            grade=plan.target.grade,
            settled_at=settled_at,
            result=plan.target.result,
            settlement_fingerprint=plan.settlement_fingerprint,
            active_bet_ids=active_bet_ids,
            active_stake_total=plan.target.active_stake_total,
            applied_odds=plan.applied_odds,
            payouts=tuple(
                MatchSettlementPayout(
                    persona_id=payout.persona_id,
                    bet_ids=payout.bet_ids,
                    amount=payout.amount,
                    point_transaction_id=payout_transaction_ids[payout.persona_id],
                )
                for payout in plan.payouts
            ),
            rewards=tuple(
                MatchSettlementReward(
                    persona_id=reward.persona_id,
                    selected_match_entry_id=reward.selected_match_entry_id,
                    selected_game_account_id=reward.selected_game_account_id,
                    selected_rank=reward.selected_rank,
                    suppressed_match_entry_ids=reward.suppressed_match_entry_ids,
                    amount=reward.amount,
                    point_transaction_id=reward_transaction_ids[reward.persona_id],
                )
                for reward in plan.rewards
            ),
            rating_rule_version=plan.target.rating_rule_version,
            ratings=tuple(
                MatchSettlementRating(
                    match_entry_id=rating.match_entry_id,
                    entry_number=rating.entry_number,
                    game_account_id=rating.game_account_id,
                    game_account_name=rating.game_account_name,
                    horse_name=rating.horse_name,
                    affiliation_at_event=rating.affiliation_at_event,
                    rank=rating.rank,
                    rating_disposition=rating.rating_disposition,
                    rating_rank=rating.rating_rank,
                    rating_before=rating.rating_before,
                    base_delta=rating.base_delta,
                    adjustment_delta=rating.adjustment_delta,
                    amount=rating.amount,
                    rating_after=rating.rating_after,
                    rating_transaction_id=rating_transaction_ids.get(rating.match_entry_id),
                )
                for rating in plan.ratings
            ),
        )
        self._session.add(
            MatchOperationORM(
                operation_id=operation.id,
                match_id=plan.target.match_id,
                type=MatchSettlementAuditType.SETTLED.value,
                before_data=plan.target.to_audit_payload(
                    excluded_rating_entry_ids=plan.excluded_rating_entry_ids,
                ),
                after_data=settled.to_audit_payload(),
            )
        )
        self._session.flush()
        return settled

    def load_result_publication_source(
        self,
        *,
        match_id: int,
        guild_id: str,
    ) -> MatchResultPublicationSource | None:
        target = self._result_publications.load_target(match_id=match_id, guild_id=guild_id)
        return None if target is None else target.to_publication_source()

    def add_result_publication(
        self,
        *,
        intent: PublicationIntent,
        created_at: datetime,
    ) -> None:
        self._result_publications.add_publication(intent=intent, created_at=created_at)


class SqlAlchemyMatchSettlementQueryRepository:
    """Build bounded settlement choices and complete read-only Previews."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchSettlementTargetChoice, ...]:
        entry_count = (
            select(func.count(MatchEntryORM.id))
            .where(MatchEntryORM.match_id == MatchORM.id)
            .correlate(MatchORM)
            .scalar_subquery()
        )
        active_bet_count = (
            select(func.count(BetORM.id))
            .where(
                BetORM.match_id == MatchORM.id,
                BetORM.status == BetStatus.ACTIVE.value,
                BetORM.active_marker.is_(True),
            )
            .correlate(MatchORM)
            .scalar_subquery()
        )
        statement = select(
            MatchORM.id,
            MatchORM.name,
            entry_count.label("entry_count"),
            active_bet_count.label("active_bet_count"),
        ).where(
            MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
            MatchORM.status == MatchStatus.RESULT_CONFIRMED.value,
            MatchORM.terminal_reason.is_(None),
        )
        if search:
            statement = statement.where(MatchORM.name.contains(search))
        rows = self._session.execute(statement.order_by(MatchORM.scheduled_at, MatchORM.id).limit(limit))
        return tuple(
            MatchSettlementTargetChoice(
                match_id=row.id,
                match_name=row.name,
                entry_count=_integer_value(row.entry_count, field_name="entry_count"),
                active_bet_count=_integer_value(row.active_bet_count, field_name="active_bet_count"),
            )
            for row in rows
        )

    def load_target(self, *, match_id: int) -> MatchSettlementTarget | None:
        return load_match_settlement_target(self._session, match_id=match_id, lock=False)


class SqlAlchemyMatchSettlementUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete command UoW exposing Match settlement persistence."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._repository: SqlAlchemyMatchSettlementRepository | None = None

    @property
    def match_settlement(self) -> SqlAlchemyMatchSettlementRepository:
        return self._require_active_repository(self._repository)

    def _activate_repositories(self) -> None:
        self._repository = SqlAlchemyMatchSettlementRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._repository = None


class SqlAlchemyMatchSettlementUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchSettlementUnitOfWork]
):
    """Create one fresh Match settlement command UoW."""

    unit_of_work_type = SqlAlchemyMatchSettlementUnitOfWork


class SqlAlchemyMatchSettlementQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW exposing Match settlement projections."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._repository: SqlAlchemyMatchSettlementQueryRepository | None = None

    @property
    def match_settlement_queries(self) -> SqlAlchemyMatchSettlementQueryRepository:
        return self._require_active_repository(self._repository)

    def _activate_repositories(self) -> None:
        self._repository = SqlAlchemyMatchSettlementQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._repository = None


class SqlAlchemyMatchSettlementQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchSettlementQueryUnitOfWork]
):
    """Create one fresh Match settlement query UoW."""

    unit_of_work_type = SqlAlchemyMatchSettlementQueryUnitOfWork
