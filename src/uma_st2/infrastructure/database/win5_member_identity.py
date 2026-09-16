"""Shared Persona resolution for WIN5 member persistence adapters."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from uma_st2.application.win5.member_identity import Win5MemberPersona
from uma_st2.domain.identity import PersonaStatus

from .orm import DiscordAccountORM, GameAccountORM, PersonaORM


def find_win5_member_persona(
    session: Session,
    *,
    discord_user_id: str,
) -> Win5MemberPersona | None:
    """Resolve one DiscordAccount to its canonical Persona status projection."""

    eligible_account = (
        select(GameAccountORM.id)
        .where(
            GameAccountORM.persona_id == PersonaORM.id,
            GameAccountORM.uma_pid.is_not(None),
        )
        .exists()
    )
    row = session.execute(
        select(
            PersonaORM.id,
            PersonaORM.status,
            eligible_account.label("has_eligible_game_account"),
        )
        .join(DiscordAccountORM, DiscordAccountORM.persona_id == PersonaORM.id)
        .where(DiscordAccountORM.discord_user_id == discord_user_id)
    ).one_or_none()
    if row is None:
        return None
    return Win5MemberPersona(
        id=row.id,
        status=PersonaStatus(row.status),
        has_eligible_game_account=row.has_eligible_game_account,
    )


def lock_win5_member_persona(
    session: Session,
    *,
    discord_user_id: str,
) -> Win5MemberPersona | None:
    """Lock one member identity and its first qualifying PID-bearing account."""

    discord_account = session.scalar(
        select(DiscordAccountORM).where(DiscordAccountORM.discord_user_id == discord_user_id).with_for_update()
    )
    if discord_account is None or discord_account.persona_id is None:
        return None
    persona = session.scalar(select(PersonaORM).where(PersonaORM.id == discord_account.persona_id).with_for_update())
    if persona is None:
        raise ValueError("DiscordAccount references a missing Persona.")
    eligible_account_id = session.scalar(
        select(GameAccountORM.id)
        .where(
            GameAccountORM.persona_id == persona.id,
            GameAccountORM.uma_pid.is_not(None),
        )
        .order_by(GameAccountORM.id)
        .limit(1)
        .with_for_update()
    )
    return Win5MemberPersona(
        id=persona.id,
        status=PersonaStatus(persona.status),
        has_eligible_game_account=eligible_account_id is not None,
    )
