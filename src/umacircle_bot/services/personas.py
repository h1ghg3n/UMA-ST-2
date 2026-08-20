from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import DiscordAccount, GameAccount, Persona
from umacircle_bot.domain.errors import AccountNotFoundError, AccountOwnershipError
from umacircle_bot.domain.personas import normalize_persona_id
from umacircle_bot.services.dtos import GameAccountDTO, PersonaDTO


def resolve_persona(session: Session, *, persona_id: str) -> PersonaDTO:
    normalized_persona_id = normalize_persona_id(persona_id)
    persona = session.scalar(select(Persona).where(Persona.id == normalized_persona_id))
    if persona is None:
        raise AccountNotFoundError("Persona not found")
    return _persona_dto(persona)


def resolve_persona_for_discord_account(session: Session, *, discord_user_id: str) -> PersonaDTO:
    persona = session.scalar(
        select(Persona)
        .join(DiscordAccount, DiscordAccount.persona_id == Persona.id)
        .where(DiscordAccount.discord_user_id == discord_user_id)
    )
    if persona is None:
        raise AccountNotFoundError("Discord account is not linked to a Persona")
    return _persona_dto(persona)


def resolve_persona_for_game_account(session: Session, *, game_account_id: int) -> PersonaDTO:
    persona = session.scalar(
        select(Persona).join(GameAccount, GameAccount.persona_id == Persona.id).where(GameAccount.id == game_account_id)
    )
    if persona is None:
        raise AccountNotFoundError("game account is not linked to a Persona")
    return _persona_dto(persona)


def list_persona_game_accounts(session: Session, *, persona_id: str) -> tuple[GameAccountDTO, ...]:
    """Return every peer GameAccount in stable ID order."""

    normalized_persona_id = normalize_persona_id(persona_id)
    if session.get(Persona, normalized_persona_id) is None:
        raise AccountNotFoundError("Persona not found")
    game_accounts = session.scalars(
        select(GameAccount).where(GameAccount.persona_id == normalized_persona_id).order_by(GameAccount.id)
    )
    return tuple(_game_account_dto(game_account) for game_account in game_accounts)


def assert_game_account_belongs_to_persona(
    session: Session,
    *,
    game_account_id: int,
    persona_id: str,
) -> None:
    normalized_persona_id = normalize_persona_id(persona_id)
    game_account = session.get(GameAccount, game_account_id)
    if game_account is None:
        raise AccountNotFoundError("game account not found")
    if game_account.persona_id != normalized_persona_id:
        raise AccountOwnershipError("game account does not belong to Persona")


def _persona_dto(persona: Persona) -> PersonaDTO:
    return PersonaDTO(
        id=persona.id,
        display_name=persona.display_name,
        display_name_source=persona.display_name_source,
        status=persona.status,
    )


def _game_account_dto(game_account: GameAccount) -> GameAccountDTO:
    return GameAccountDTO(
        id=game_account.id,
        persona_id=game_account.persona_id,
        discord_account_id=game_account.discord_account_id,
        uma_pid=game_account.uma_pid,
        nickname=game_account.nickname,
        ingame_name=game_account.ingame_name,
        identity_status=game_account.identity_status,
    )
