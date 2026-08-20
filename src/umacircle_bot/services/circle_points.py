from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from umacircle_bot.db.models import CirclePointTransaction, GameAccount
from umacircle_bot.domain.betting import MAX_POINT_AMOUNT, validate_circle_point_balance, validate_positive_point_amount
from umacircle_bot.domain.errors import AccountNotFoundError, BettingRuleError
from umacircle_bot.domain.identity import normalize_uma_pid
from umacircle_bot.domain.time import database_datetime_as_utc
from umacircle_bot.services.dtos import CirclePointTransactionDTO
from umacircle_bot.services.persona_wallets import lock_persona_wallet_for_game_account


def grant_circle_points(
    session: Session,
    *,
    uma_pid: str,
    amount: int,
    reason: str,
    granted_by_discord_user_id: str,
    idempotency_key: str | None = None,
) -> CirclePointTransactionDTO:
    normalized_pid = normalize_uma_pid(uma_pid)
    validate_positive_point_amount(amount, field_name="grant amount")
    normalized_reason = _normalize_reason(reason)
    actor_id = _normalize_discord_user_id(granted_by_discord_user_id)
    normalized_idempotency_key = _normalize_idempotency_key(idempotency_key)
    expected_game_account_id: int | None = None
    try:
        with session.begin_nested():
            game_account = session.scalar(
                select(GameAccount).where(GameAccount.uma_pid == normalized_pid).with_for_update()
            )
            if game_account is None:
                raise AccountNotFoundError("game account not found")
            expected_game_account_id = game_account.id
            if normalized_idempotency_key is not None:
                existing = _load_idempotent_grant(
                    session,
                    idempotency_key=normalized_idempotency_key,
                    game_account_id=game_account.id,
                    amount=amount,
                    reason=normalized_reason,
                    actor_id=actor_id,
                    lock_row=False,
                )
                if existing is not None:
                    return _transaction_dto(existing)
            _locked_game_account, point_account = lock_persona_wallet_for_game_account(
                session, game_account_id=game_account.id
            )
            validate_circle_point_balance(point_account.balance + amount)
            point_account.balance += amount
            transaction = CirclePointTransaction(
                persona_id=point_account.persona_id,
                game_account_id=game_account.id,
                type="admin_grant",
                amount=amount,
                reason=normalized_reason,
                source="operator_grant",
                created_by_discord_user_id=actor_id,
                idempotency_key=normalized_idempotency_key,
            )
            session.add(transaction)
            session.flush()
    except IntegrityError as exc:
        if normalized_idempotency_key is None or expected_game_account_id is None:
            raise
        with session.begin_nested():
            existing = _load_idempotent_grant(
                session,
                idempotency_key=normalized_idempotency_key,
                game_account_id=expected_game_account_id,
                amount=amount,
                reason=normalized_reason,
                actor_id=actor_id,
                lock_row=True,
            )
            if existing is None:
                raise exc
            return _transaction_dto(existing)
    return _transaction_dto(transaction)


def adjust_circle_points(
    session: Session,
    *,
    uma_pid: str,
    amount: int,
    reason: str,
    adjusted_by_discord_user_id: str,
    idempotency_key: str | None = None,
) -> CirclePointTransactionDTO:
    """Apply an operator-approved signed adjustment and preserve its ledger entry."""
    normalized_pid = normalize_uma_pid(uma_pid)
    _validate_signed_adjustment_amount(amount)
    normalized_reason = _normalize_reason(reason)
    actor_id = _normalize_discord_user_id(adjusted_by_discord_user_id)
    normalized_idempotency_key = _normalize_idempotency_key(idempotency_key)
    expected_game_account_id: int | None = None
    try:
        with session.begin_nested():
            game_account = session.scalar(
                select(GameAccount).where(GameAccount.uma_pid == normalized_pid).with_for_update()
            )
            if game_account is None:
                raise AccountNotFoundError("game account not found")
            expected_game_account_id = game_account.id
            if normalized_idempotency_key is not None:
                existing = _load_idempotent_adjustment(
                    session,
                    idempotency_key=normalized_idempotency_key,
                    game_account_id=game_account.id,
                    amount=amount,
                    reason=normalized_reason,
                    actor_id=actor_id,
                    lock_row=False,
                )
                if existing is not None:
                    return _transaction_dto(existing)
            _locked_game_account, point_account = lock_persona_wallet_for_game_account(
                session, game_account_id=game_account.id
            )
            validate_circle_point_balance(point_account.balance + amount)
            point_account.balance += amount
            transaction = CirclePointTransaction(
                persona_id=point_account.persona_id,
                game_account_id=game_account.id,
                type="admin_adjustment",
                amount=amount,
                reason=normalized_reason,
                source="operator_adjustment",
                created_by_discord_user_id=actor_id,
                idempotency_key=normalized_idempotency_key,
            )
            session.add(transaction)
            session.flush()
    except IntegrityError as exc:
        if normalized_idempotency_key is None or expected_game_account_id is None:
            raise
        with session.begin_nested():
            existing = _load_idempotent_adjustment(
                session,
                idempotency_key=normalized_idempotency_key,
                game_account_id=expected_game_account_id,
                amount=amount,
                reason=normalized_reason,
                actor_id=actor_id,
                lock_row=True,
            )
            if existing is None:
                raise exc
            return _transaction_dto(existing)
    return _transaction_dto(transaction)


def _transaction_dto(transaction: CirclePointTransaction) -> CirclePointTransactionDTO:
    return CirclePointTransactionDTO(
        id=transaction.id,
        persona_id=transaction.persona_id,
        game_account_id=transaction.game_account_id,
        type=transaction.type,
        amount=transaction.amount,
        reason=transaction.reason,
        source=transaction.source,
        related_bet_id=transaction.related_bet_id,
        related_race_result_id=transaction.related_race_result_id,
        created_by_discord_user_id=transaction.created_by_discord_user_id,
        created_at=database_datetime_as_utc(transaction.created_at),
    )


def _load_idempotent_grant(
    session: Session,
    *,
    idempotency_key: str,
    game_account_id: int,
    amount: int,
    reason: str,
    actor_id: str,
    lock_row: bool,
) -> CirclePointTransaction | None:
    query = select(CirclePointTransaction).where(CirclePointTransaction.idempotency_key == idempotency_key)
    if lock_row:
        query = query.with_for_update()
    transaction = session.scalar(query)
    if transaction is None:
        return None
    if (
        transaction.game_account_id != game_account_id
        or transaction.type != "admin_grant"
        or transaction.amount != amount
        or transaction.reason != reason
        or transaction.source != "operator_grant"
        or transaction.created_by_discord_user_id != actor_id
        or transaction.related_bet_id is not None
    ):
        raise BettingRuleError("point grant idempotency key payload does not match")
    return transaction


def _load_idempotent_adjustment(
    session: Session,
    *,
    idempotency_key: str,
    game_account_id: int,
    amount: int,
    reason: str,
    actor_id: str,
    lock_row: bool,
) -> CirclePointTransaction | None:
    query = select(CirclePointTransaction).where(CirclePointTransaction.idempotency_key == idempotency_key)
    if lock_row:
        query = query.with_for_update()
    transaction = session.scalar(query)
    if transaction is None:
        return None
    if (
        transaction.game_account_id != game_account_id
        or transaction.type != "admin_adjustment"
        or transaction.amount != amount
        or transaction.reason != reason
        or transaction.source != "operator_adjustment"
        or transaction.created_by_discord_user_id != actor_id
        or transaction.related_bet_id is not None
    ):
        raise BettingRuleError("point adjustment idempotency key payload does not match")
    return transaction


def _validate_signed_adjustment_amount(value: int) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not -MAX_POINT_AMOUNT <= value <= MAX_POINT_AMOUNT
        or value == 0
    ):
        raise BettingRuleError(
            f"adjustment amount must be an integer between {-MAX_POINT_AMOUNT} and {MAX_POINT_AMOUNT}, excluding zero"
        )


def _normalize_reason(value: str) -> str:
    if not isinstance(value, str):
        raise BettingRuleError("grant reason must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > 255 or any(ord(character) < 32 for character in normalized):
        raise BettingRuleError("grant reason must contain 1 to 255 printable characters")
    return normalized


def _normalize_discord_user_id(value: str) -> str:
    if not isinstance(value, str):
        raise BettingRuleError("Discord user ID must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > 32 or not normalized.isascii() or not normalized.isdigit():
        raise BettingRuleError("Discord user ID must contain 1 to 32 ASCII digits")
    return normalized


def _normalize_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BettingRuleError("point grant idempotency key must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > 128 or any(ord(character) < 32 for character in normalized):
        raise BettingRuleError("point grant idempotency key must contain 1 to 128 printable characters")
    return normalized
