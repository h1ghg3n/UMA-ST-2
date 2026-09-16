"""MariaDB evidence for native V2 WIN5 member eligibility projection and lock."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.engine import Engine

from uma_st2.infrastructure.database import DatabaseRuntime
from uma_st2.infrastructure.database.orm import DiscordAccountORM, GameAccountORM, PersonaORM
from uma_st2.infrastructure.database.win5_member_identity import (
    find_win5_member_persona,
    lock_win5_member_persona,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 30, 12, 0)


def test_member_eligibility_requires_an_owned_non_null_pid_account(migrated_engine: Engine) -> None:
    runtime = DatabaseRuntime.from_engine(migrated_engine)
    suffix = uuid4().hex
    persona_id = str(uuid4())
    discord_user_id = str(int(suffix[:15], 16) + 1)

    with migrated_engine.begin() as connection:
        connection.execute(
            PersonaORM.__table__.insert().values(
                id=persona_id,
                display_name=f"WIN5 member {suffix}",
                status="normal",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        connection.execute(
            DiscordAccountORM.__table__.insert().values(
                discord_user_id=discord_user_id,
                persona_id=persona_id,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        account_id = connection.execute(
            GameAccountORM.__table__.insert().values(
                persona_id=persona_id,
                game_region="KR",
                uma_pid=None,
                nickname=f"Historical account {suffix}",
                affiliation=None,
                created_at=NOW,
                updated_at=NOW,
            )
        ).inserted_primary_key[0]

    with runtime.session_factory.begin() as session:
        projected = find_win5_member_persona(session, discord_user_id=discord_user_id)
        locked = lock_win5_member_persona(session, discord_user_id=discord_user_id)

    assert projected is not None
    assert projected.is_active is True
    assert projected.has_eligible_game_account is False
    assert projected.is_eligible is False
    assert locked is not None
    assert locked.has_eligible_game_account is False

    with migrated_engine.begin() as connection:
        connection.execute(
            update(GameAccountORM).where(GameAccountORM.id == account_id).values(uma_pid=f"9{suffix[:15]}")
        )

    with runtime.session_factory.begin() as session:
        projected = find_win5_member_persona(session, discord_user_id=discord_user_id)
        locked = lock_win5_member_persona(session, discord_user_id=discord_user_id)

    assert projected is not None
    assert projected.has_eligible_game_account is True
    assert projected.is_eligible is True
    assert locked is not None
    assert locked.has_eligible_game_account is True
    assert locked.is_eligible is True
