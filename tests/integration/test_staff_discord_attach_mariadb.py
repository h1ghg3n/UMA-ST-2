"""MariaDB atomicity and serialization evidence for direct Discord attachment."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine

from uma_st2.application.identity import (
    AttachDiscordAccountToPersona,
    StaffDiscordAttachConcurrentConflictError,
    StaffDiscordAttachTargetLinkedError,
)
from uma_st2.compose import (
    compose_staff_discord_attach_commands,
    compose_staff_discord_attach_queries,
)
from uma_st2.infrastructure.database import DatabaseRuntime
from uma_st2.infrastructure.database.orm import (
    CirclePointORM,
    DiscordAccountORM,
    DiscordPublicationORM,
    GameAccountORM,
    IdentityOperationORM,
    OperationORM,
    PersonaORM,
    PointTransactionORM,
)

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)


def _suffix() -> str:
    return str(uuid4().int % 10**17).zfill(17)


def _seed_personas(engine: Engine, *, persona_ids: tuple[str, ...], with_eligibility: bool) -> None:
    with engine.begin() as connection:
        for index, persona_id in enumerate(persona_ids):
            connection.execute(
                PersonaORM.__table__.insert().values(
                    id=persona_id,
                    display_name=f"MariaDB Persona {index + 1}",
                    status="normal",
                    created_at=NOW.replace(tzinfo=None),
                    updated_at=NOW.replace(tzinfo=None),
                )
            )
            if with_eligibility:
                connection.execute(
                    GameAccountORM.__table__.insert().values(
                        persona_id=persona_id,
                        game_region="KR",
                        uma_pid=f"7{_suffix()}",
                        nickname=f"MariaDB 계정 {index + 1}",
                        affiliation=None,
                        created_at=NOW.replace(tzinfo=None),
                        updated_at=NOW.replace(tzinfo=None),
                    )
                )
                connection.execute(
                    CirclePointORM.__table__.insert().values(
                        persona_id=persona_id,
                        balance=700,
                        updated_at=NOW.replace(tzinfo=None),
                    )
                )


def _command(
    engine: Engine,
    *,
    guild_id: str,
    persona_id: str,
    discord_user_id: str,
    key: str,
) -> AttachDiscordAccountToPersona:
    runtime = DatabaseRuntime.from_engine(engine)
    preview = compose_staff_discord_attach_queries(runtime).get_preview(
        guild_id=guild_id,
        persona_id=persona_id,
        discord_user_id=discord_user_id,
    )
    return AttachDiscordAccountToPersona(
        guild_id=guild_id,
        target_persona_id=persona_id,
        target_discord_user_id=discord_user_id,
        attached_by_discord_user_id=f"8{_suffix()}",
        expected_target_fingerprint=preview.state_fingerprint,
        idempotency_key=key,
        correlation_id=key,
        operational_note="MariaDB direct attach",
    )


def _cleanup(
    engine: Engine,
    *,
    guild_id: str,
    persona_ids: tuple[str, ...],
    discord_user_id: str,
) -> None:
    with engine.begin() as connection:
        operation_ids = tuple(connection.scalars(select(OperationORM.id).where(OperationORM.guild_id == guild_id)))
        if operation_ids:
            connection.execute(delete(IdentityOperationORM).where(IdentityOperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(PointTransactionORM).where(PointTransactionORM.operation_id.in_(operation_ids)))
        connection.execute(delete(DiscordPublicationORM).where(DiscordPublicationORM.guild_id == guild_id))
        connection.execute(delete(DiscordAccountORM).where(DiscordAccountORM.discord_user_id == discord_user_id))
        connection.execute(delete(GameAccountORM).where(GameAccountORM.persona_id.in_(persona_ids)))
        connection.execute(delete(CirclePointORM).where(CirclePointORM.persona_id.in_(persona_ids)))
        if operation_ids:
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(delete(PersonaORM).where(PersonaORM.id.in_(persona_ids)))


def test_direct_attach_commits_access_and_audit_only_with_exact_retry(
    migrated_engine: Engine,
) -> None:
    suffix = _suffix()
    guild_id = f"6{suffix}"
    persona_id = str(uuid4())
    discord_user_id = f"9{suffix}"
    try:
        _seed_personas(migrated_engine, persona_ids=(persona_id,), with_eligibility=True)
        command = _command(
            migrated_engine,
            guild_id=guild_id,
            persona_id=persona_id,
            discord_user_id=discord_user_id,
            key=f"staff-discord-attach-{suffix}",
        )
        commands = compose_staff_discord_attach_commands(DatabaseRuntime.from_engine(migrated_engine))

        created = commands.attach(command)
        retried = commands.attach(command)

        assert created.member_mutation_eligible is True
        assert retried.exact_retry is True
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(DiscordAccountORM.persona_id).where(DiscordAccountORM.discord_user_id == discord_user_id)
                )
                == persona_id
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(IdentityOperationORM)
                    .join(OperationORM, OperationORM.id == IdentityOperationORM.operation_id)
                    .where(
                        OperationORM.guild_id == guild_id,
                        IdentityOperationORM.type == "discord_account_attached",
                    )
                )
                == 1
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(PointTransactionORM)
                    .where(PointTransactionORM.persona_id == persona_id)
                )
                == 0
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(DiscordPublicationORM)
                    .where(DiscordPublicationORM.guild_id == guild_id)
                )
                == 0
            )
    finally:
        _cleanup(
            migrated_engine,
            guild_id=guild_id,
            persona_ids=(persona_id,),
            discord_user_id=discord_user_id,
        )


def test_concurrent_same_discord_attach_serializes_to_one_persona(
    migrated_engine: Engine,
) -> None:
    suffix = _suffix()
    guild_id = f"6{suffix}"
    persona_ids = (str(uuid4()), str(uuid4()))
    discord_user_id = f"9{suffix}"
    barrier = Barrier(2)
    try:
        _seed_personas(migrated_engine, persona_ids=persona_ids, with_eligibility=False)
        commands = tuple(
            _command(
                migrated_engine,
                guild_id=guild_id,
                persona_id=persona_id,
                discord_user_id=discord_user_id,
                key=f"staff-discord-attach-{suffix}-{index}",
            )
            for index, persona_id in enumerate(persona_ids)
        )

        def attach(command: AttachDiscordAccountToPersona) -> str:
            service = compose_staff_discord_attach_commands(DatabaseRuntime.from_engine(migrated_engine))
            barrier.wait()
            try:
                service.attach(command)
            except StaffDiscordAttachTargetLinkedError:
                return "target-linked"
            except StaffDiscordAttachConcurrentConflictError:
                return "concurrent-conflict"
            return "attached"

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures: tuple[Future[str], ...] = tuple(executor.submit(attach, command) for command in commands)
            outcomes = tuple(future.result(timeout=20) for future in futures)

        assert outcomes.count("attached") == 1
        assert len({*outcomes} - {"attached"}) == 1
        assert ({*outcomes} - {"attached"}).pop() in {
            "target-linked",
            "concurrent-conflict",
        }
        with migrated_engine.connect() as connection:
            owner = connection.scalar(
                select(DiscordAccountORM.persona_id).where(DiscordAccountORM.discord_user_id == discord_user_id)
            )
            assert owner in persona_ids
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(IdentityOperationORM)
                    .join(OperationORM, OperationORM.id == IdentityOperationORM.operation_id)
                    .where(
                        OperationORM.guild_id == guild_id,
                        IdentityOperationORM.type == "discord_account_attached",
                    )
                )
                == 1
            )
    finally:
        _cleanup(
            migrated_engine,
            guild_id=guild_id,
            persona_ids=persona_ids,
            discord_user_id=discord_user_id,
        )
