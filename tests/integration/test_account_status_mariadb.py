"""MariaDB evidence for the private Account status projection boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from uma_st2.application.execution import QueryRunner
from uma_st2.application.identity import AccountEligibilityState, AccountStatusQueries
from uma_st2.infrastructure.database import SqlAlchemyAccountStatusQueryUnitOfWorkFactory
from uma_st2.infrastructure.database.orm import (
    CirclePointORM,
    DiscordAccountORM,
    GameAccountORM,
    PersonaORM,
    RatingORM,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 2, 3, 0, tzinfo=UTC)
DB_NOW = NOW.replace(tzinfo=None)


def test_account_status_reads_mariadb_identity_wallet_and_global_rating_rank(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex[:12]
    persona_ids = (f"account-status-{suffix}-a", f"account-status-{suffix}-b")
    discord_user_id = f"90{suffix}"
    game_account_ids: list[int] = []

    try:
        with migrated_engine.begin() as connection:
            connection.execute(
                PersonaORM.__table__.insert(),
                [
                    {
                        "id": persona_ids[0],
                        "display_name": "계정 상태 사용자",
                        "status": "normal",
                        "created_at": DB_NOW,
                        "updated_at": DB_NOW,
                    },
                    {
                        "id": persona_ids[1],
                        "display_name": "계정 상태 비교군",
                        "status": "normal",
                        "created_at": DB_NOW,
                        "updated_at": DB_NOW,
                    },
                ],
            )
            connection.execute(
                DiscordAccountORM.__table__.insert().values(
                    discord_user_id=discord_user_id,
                    persona_id=persona_ids[0],
                    created_at=DB_NOW,
                    updated_at=DB_NOW,
                )
            )
            for persona_id, pid, nickname in (
                (persona_ids[0], f"71{suffix}", "조회 대상 계정"),
                (persona_ids[1], f"72{suffix}", "비교 계정"),
            ):
                result = connection.execute(
                    GameAccountORM.__table__.insert().values(
                        persona_id=persona_id,
                        game_region="KR",
                        uma_pid=pid,
                        nickname=nickname,
                        affiliation=None,
                        created_at=DB_NOW,
                        updated_at=DB_NOW,
                    )
                )
                game_account_ids.append(int(result.inserted_primary_key[0]))
            connection.execute(
                CirclePointORM.__table__.insert().values(
                    persona_id=persona_ids[0],
                    balance=650,
                    updated_at=DB_NOW,
                )
            )
            connection.execute(
                RatingORM.__table__.insert(),
                [
                    {
                        "game_account_id": game_account_ids[0],
                        "rating": Decimal("1550.000000000000000000"),
                        "updated_at": DB_NOW,
                    },
                    {
                        "game_account_id": game_account_ids[1],
                        "rating": Decimal("1600.000000000000000000"),
                        "updated_at": DB_NOW,
                    },
                ],
            )

        queries = AccountStatusQueries(
            QueryRunner(SqlAlchemyAccountStatusQueryUnitOfWorkFactory(sessionmaker(bind=migrated_engine))),
            clock=lambda: NOW,
        )

        overview = queries.get_overview(discord_user_id=discord_user_id, guild_id="987")
        identity = queries.get_identity_details(discord_user_id=discord_user_id, guild_id="987")

        assert overview.display_name == "계정 상태 사용자"
        assert overview.wallet_balance == 650
        assert overview.eligibility is AccountEligibilityState.ELIGIBLE
        assert overview.match.entry_count == 0
        assert identity.total_count == 1
        assert identity.accounts[0].pid_hint == f"••••{suffix[-4:]}"
        assert identity.accounts[0].current_rating == Decimal("1550.000000000000000000")
        assert identity.accounts[0].competition_rank == 2
    finally:
        with migrated_engine.begin() as connection:
            if game_account_ids:
                connection.execute(delete(RatingORM).where(RatingORM.game_account_id.in_(game_account_ids)))
            connection.execute(delete(CirclePointORM).where(CirclePointORM.persona_id.in_(persona_ids)))
            connection.execute(delete(GameAccountORM).where(GameAccountORM.persona_id.in_(persona_ids)))
            connection.execute(delete(DiscordAccountORM).where(DiscordAccountORM.persona_id.in_(persona_ids)))
            connection.execute(delete(PersonaORM).where(PersonaORM.id.in_(persona_ids)))
