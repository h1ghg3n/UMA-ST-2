from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import CirclePointAccount, GameAccount
from umacircle_bot.domain.errors import AccountNotFoundError


def lock_persona_wallet_for_game_account(
    session: Session,
    *,
    game_account_id: int,
) -> tuple[GameAccount, CirclePointAccount]:
    """Lock a GameAccount and its canonical Persona-owned Circle Point wallet."""
    game_account = session.scalar(select(GameAccount).where(GameAccount.id == game_account_id).with_for_update())
    if game_account is None:
        raise AccountNotFoundError("game account not found")
    if game_account.persona_id is None:
        raise AccountNotFoundError("game account has no Persona owner")
    point_account = session.scalar(
        select(CirclePointAccount).where(CirclePointAccount.persona_id == game_account.persona_id).with_for_update()
    )
    if point_account is None:
        raise AccountNotFoundError("room point account not found")
    return game_account, point_account


def lock_persona_wallets_for_game_accounts(
    session: Session,
    *,
    game_account_ids: Iterable[int],
) -> dict[int, tuple[GameAccount, CirclePointAccount]]:
    """Lock wallets in canonical Persona order and map each provenance account."""
    account_ids = tuple(sorted(set(game_account_ids)))
    if not account_ids:
        return {}
    game_accounts = list(
        session.scalars(
            select(GameAccount)
            .where(GameAccount.id.in_(account_ids))
            .order_by(GameAccount.persona_id, GameAccount.id)
            .with_for_update()
        )
    )
    if len(game_accounts) != len(account_ids) or any(account.persona_id is None for account in game_accounts):
        raise AccountNotFoundError("game account or Persona owner not found")
    persona_ids = tuple(sorted({account.persona_id for account in game_accounts if account.persona_id is not None}))
    point_accounts = {
        point_account.persona_id: point_account
        for point_account in session.scalars(
            select(CirclePointAccount)
            .where(CirclePointAccount.persona_id.in_(persona_ids))
            .order_by(CirclePointAccount.persona_id)
            .with_for_update()
        )
    }
    if set(point_accounts) != set(persona_ids):
        raise AccountNotFoundError("room point account not found")
    return {account.id: (account, point_accounts[account.persona_id]) for account in game_accounts}


def lock_persona_wallets(
    session: Session,
    *,
    persona_ids: Iterable[str],
) -> dict[str, CirclePointAccount]:
    """Lock immutable Bet-owner wallets in canonical Persona order."""

    normalized_ids = tuple(sorted(set(persona_ids)))
    if not normalized_ids:
        return {}
    wallets = {
        wallet.persona_id: wallet
        for wallet in session.scalars(
            select(CirclePointAccount)
            .where(CirclePointAccount.persona_id.in_(normalized_ids))
            .order_by(CirclePointAccount.persona_id)
            .with_for_update()
        )
    }
    if set(wallets) != set(normalized_ids):
        raise AccountNotFoundError("room point account not found")
    return wallets
