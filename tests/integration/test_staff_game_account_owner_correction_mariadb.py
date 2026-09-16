"""MariaDB atomicity and serialization evidence for GameAccount owner correction."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine

from uma_st2.application.identity import (
    CorrectGameAccountOwner,
    StaffGameAccountOwnerCorrectionConcurrentConflictError,
    StaffGameAccountOwnerCorrectionStaleError,
    StaffGameAccountOwnerCorrectionWalletMissingError,
)
from uma_st2.compose import (
    compose_staff_game_account_owner_correction_commands,
    compose_staff_game_account_owner_correction_queries,
)
from uma_st2.domain.identity import GameRegion
from uma_st2.infrastructure.database import DatabaseRuntime
from uma_st2.infrastructure.database.orm import (
    CirclePointORM,
    DiscordPublicationORM,
    GameAccountORM,
    IdentityOperationORM,
    OperationORM,
    PersonaORM,
    PointTransactionORM,
)

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _suffix() -> str:
    return str(uuid4().int % 10**17).zfill(17)


def _seed(
    engine: Engine,
    *,
    source_id: str,
    target_ids: tuple[str, ...],
    account_pid: str,
    missing_wallet_id: str | None = None,
) -> None:
    persona_ids = (source_id, *target_ids)
    with engine.begin() as connection:
        for index, persona_id in enumerate(persona_ids):
            connection.execute(
                PersonaORM.__table__.insert().values(
                    id=persona_id,
                    display_name=f"MariaDB owner {index + 1}",
                    status="normal" if index != 1 else "warning",
                    created_at=NOW.replace(tzinfo=None),
                    updated_at=NOW.replace(tzinfo=None),
                )
            )
            if persona_id != missing_wallet_id:
                connection.execute(
                    CirclePointORM.__table__.insert().values(
                        persona_id=persona_id,
                        balance=700 + index * 100,
                        updated_at=NOW.replace(tzinfo=None),
                    )
                )
        connection.execute(
            GameAccountORM.__table__.insert().values(
                persona_id=source_id,
                game_region="KR",
                uma_pid=account_pid,
                nickname="MariaDB 이동 계정",
                affiliation="통합 테스트",
                created_at=NOW.replace(tzinfo=None),
                updated_at=NOW.replace(tzinfo=None),
            )
        )
        for index, target_id in enumerate(target_ids):
            connection.execute(
                GameAccountORM.__table__.insert().values(
                    persona_id=target_id,
                    game_region="JP",
                    uma_pid=f"8{index}{_suffix()}",
                    nickname=f"타깃 계정 {index + 1}",
                    affiliation=None,
                    created_at=NOW.replace(tzinfo=None),
                    updated_at=NOW.replace(tzinfo=None),
                )
            )


def _command(
    engine: Engine,
    *,
    guild_id: str,
    target_id: str,
    account_pid: str,
    key: str,
) -> CorrectGameAccountOwner:
    runtime = DatabaseRuntime.from_engine(engine)
    preview = compose_staff_game_account_owner_correction_queries(runtime).get_preview(
        guild_id=guild_id,
        target_persona_id=target_id,
        game_region=GameRegion.KR,
        uma_pid=account_pid,
        evidence_reason="MariaDB reviewed owner correction",
    )
    return CorrectGameAccountOwner(
        guild_id=guild_id,
        target_persona_id=target_id,
        game_region=preview.state.game_region,
        uma_pid=preview.state.uma_pid,
        evidence_reason=preview.evidence_reason,
        corrected_by_discord_user_id=f"9{_suffix()}",
        expected_source_persona_id=preview.state.source_persona_id,
        expected_state_fingerprint=preview.state.state_fingerprint,
        idempotency_key=key,
        correlation_id=key,
    )


def _cleanup(engine: Engine, *, guild_id: str, persona_ids: tuple[str, ...]) -> None:
    with engine.begin() as connection:
        operation_ids = tuple(connection.scalars(select(OperationORM.id).where(OperationORM.guild_id == guild_id)))
        if operation_ids:
            connection.execute(delete(IdentityOperationORM).where(IdentityOperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(PointTransactionORM).where(PointTransactionORM.operation_id.in_(operation_ids)))
        connection.execute(delete(DiscordPublicationORM).where(DiscordPublicationORM.guild_id == guild_id))
        connection.execute(delete(GameAccountORM).where(GameAccountORM.persona_id.in_(persona_ids)))
        connection.execute(delete(CirclePointORM).where(CirclePointORM.persona_id.in_(persona_ids)))
        if operation_ids:
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(delete(PersonaORM).where(PersonaORM.id.in_(persona_ids)))


def test_owner_correction_commits_once_without_point_or_publication_write(
    migrated_engine: Engine,
) -> None:
    suffix = _suffix()
    guild_id = f"6{suffix}"
    source_id, target_id = str(uuid4()), str(uuid4())
    account_pid = f"7{suffix}"
    persona_ids = (source_id, target_id)
    try:
        _seed(
            migrated_engine,
            source_id=source_id,
            target_ids=(target_id,),
            account_pid=account_pid,
        )
        command = _command(
            migrated_engine,
            guild_id=guild_id,
            target_id=target_id,
            account_pid=account_pid,
            key=f"owner-correction-{suffix}",
        )
        service = compose_staff_game_account_owner_correction_commands(DatabaseRuntime.from_engine(migrated_engine))

        corrected = service.correct(command)
        retried = service.correct(command)

        assert corrected.target_persona_id == target_id
        assert retried.exact_retry is True
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(GameAccountORM.persona_id).where(
                        GameAccountORM.game_region == "KR",
                        GameAccountORM.uma_pid == account_pid,
                    )
                )
                == target_id
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(IdentityOperationORM)
                    .join(OperationORM, OperationORM.id == IdentityOperationORM.operation_id)
                    .where(
                        OperationORM.guild_id == guild_id,
                        IdentityOperationORM.type == "game_account_owner_reassigned",
                    )
                )
                == 1
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(PointTransactionORM)
                    .where(PointTransactionORM.persona_id.in_(persona_ids))
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
        _cleanup(migrated_engine, guild_id=guild_id, persona_ids=persona_ids)


def test_missing_target_wallet_is_zero_write_in_mariadb(migrated_engine: Engine) -> None:
    suffix = _suffix()
    guild_id = f"6{suffix}"
    source_id, target_id = str(uuid4()), str(uuid4())
    account_pid = f"7{suffix}"
    persona_ids = (source_id, target_id)
    try:
        _seed(
            migrated_engine,
            source_id=source_id,
            target_ids=(target_id,),
            account_pid=account_pid,
            missing_wallet_id=target_id,
        )

        with pytest.raises(StaffGameAccountOwnerCorrectionWalletMissingError):
            _command(
                migrated_engine,
                guild_id=guild_id,
                target_id=target_id,
                account_pid=account_pid,
                key=f"owner-correction-wallet-{suffix}",
            )

        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(select(GameAccountORM.persona_id).where(GameAccountORM.uma_pid == account_pid))
                == source_id
            )
            assert (
                connection.scalar(
                    select(func.count()).select_from(OperationORM).where(OperationORM.guild_id == guild_id)
                )
                == 0
            )
            assert (
                connection.scalar(
                    select(func.count()).select_from(CirclePointORM).where(CirclePointORM.persona_id == target_id)
                )
                == 0
            )
    finally:
        _cleanup(migrated_engine, guild_id=guild_id, persona_ids=persona_ids)


def test_concurrent_same_account_correction_has_exactly_one_winner(
    migrated_engine: Engine,
) -> None:
    suffix = _suffix()
    guild_id = f"6{suffix}"
    source_id = str(uuid4())
    target_ids = (str(uuid4()), str(uuid4()))
    persona_ids = (source_id, *target_ids)
    account_pid = f"7{suffix}"
    barrier = Barrier(2)
    try:
        _seed(
            migrated_engine,
            source_id=source_id,
            target_ids=target_ids,
            account_pid=account_pid,
        )
        commands = tuple(
            _command(
                migrated_engine,
                guild_id=guild_id,
                target_id=target_id,
                account_pid=account_pid,
                key=f"owner-correction-{suffix}-{index}",
            )
            for index, target_id in enumerate(target_ids)
        )

        def correct(command: CorrectGameAccountOwner) -> str:
            service = compose_staff_game_account_owner_correction_commands(DatabaseRuntime.from_engine(migrated_engine))
            barrier.wait()
            try:
                service.correct(command)
            except StaffGameAccountOwnerCorrectionStaleError:
                return "stale"
            except StaffGameAccountOwnerCorrectionConcurrentConflictError:
                return "concurrent-conflict"
            return "corrected"

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures: tuple[Future[str], ...] = tuple(executor.submit(correct, command) for command in commands)
            outcomes = tuple(future.result(timeout=20) for future in futures)

        assert outcomes.count("corrected") == 1
        assert len({*outcomes} - {"corrected"}) == 1
        assert ({*outcomes} - {"corrected"}).pop() in {"stale", "concurrent-conflict"}
        with migrated_engine.connect() as connection:
            owner_id = connection.scalar(select(GameAccountORM.persona_id).where(GameAccountORM.uma_pid == account_pid))
            assert owner_id in target_ids
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(IdentityOperationORM)
                    .join(OperationORM, OperationORM.id == IdentityOperationORM.operation_id)
                    .where(
                        OperationORM.guild_id == guild_id,
                        IdentityOperationORM.type == "game_account_owner_reassigned",
                    )
                )
                == 1
            )
    finally:
        _cleanup(migrated_engine, guild_id=guild_id, persona_ids=persona_ids)
