"""MariaDB atomicity and serialization evidence for Account registration requests."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine

from uma_st2.application.identity import (
    AccountRegistrationAlreadyPendingError,
    SubmitAccountRegistrationRequest,
)
from uma_st2.compose import compose_account_registration_commands
from uma_st2.domain.identity import GameRegion
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyAccountRegistrationRepository,
)
from uma_st2.infrastructure.database.orm import (
    DiscordAccountORM,
    DiscordPublicationORM,
    GameAccountORM,
    GameAccountRegistrationRequestORM,
    IdentityOperationORM,
    OperationORM,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 2, 6, 0, tzinfo=UTC)


def _command(*, suffix: str, key: str | None = None) -> SubmitAccountRegistrationRequest:
    return SubmitAccountRegistrationRequest(
        guild_id=f"8{suffix}",
        actor_discord_user_id=f"9{suffix}",
        discord_display_name_snapshot="MariaDB 요청자",
        game_region=GameRegion.KR,
        uma_pid=f"7{suffix}",
        nickname="MariaDB 계정",
        affiliation="통합 테스트",
        idempotency_key=key or f"account-register-{suffix}",
        correlation_id=key or f"account-register-{suffix}",
    )


def _cleanup(engine: Engine, *, command: SubmitAccountRegistrationRequest) -> None:
    with engine.begin() as connection:
        operation_ids = tuple(
            connection.scalars(
                select(OperationORM.id).where(OperationORM.actor_discord_user_id == command.actor_discord_user_id)
            )
        )
        if operation_ids:
            connection.execute(delete(IdentityOperationORM).where(IdentityOperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(
            delete(GameAccountRegistrationRequestORM).where(
                GameAccountRegistrationRequestORM.requester_discord_user_id == command.actor_discord_user_id
            )
        )
        connection.execute(
            delete(DiscordAccountORM).where(DiscordAccountORM.discord_user_id == command.actor_discord_user_id)
        )


def _count_for_actor(engine: Engine, model: type[object], actor_id: str) -> int:
    with engine.connect() as connection:
        if model is DiscordAccountORM:
            predicate = DiscordAccountORM.discord_user_id == actor_id
        elif model is GameAccountRegistrationRequestORM:
            predicate = GameAccountRegistrationRequestORM.requester_discord_user_id == actor_id
        elif model is OperationORM:
            predicate = OperationORM.actor_discord_user_id == actor_id
        else:
            raise AssertionError("Unsupported actor-scoped model.")
        return int(connection.scalar(select(func.count()).select_from(model).where(predicate)) or 0)


def test_request_commits_one_pending_fact_and_audit_without_approved_identity_or_publication(
    migrated_engine: Engine,
) -> None:
    suffix = str(uuid4().int % 10**17).zfill(17)
    command = _command(suffix=suffix)
    commands = compose_account_registration_commands(DatabaseRuntime.from_engine(migrated_engine))
    try:
        created = commands.submit_registration_request(command)
        retried = commands.submit_registration_request(command)

        assert created.exact_retry is False
        assert retried.exact_retry is True
        assert retried.snapshot == created.snapshot
        with migrated_engine.connect() as connection:
            request = connection.execute(
                select(
                    GameAccountRegistrationRequestORM.status,
                    GameAccountRegistrationRequestORM.discord_display_name_snapshot,
                    GameAccountRegistrationRequestORM.affiliation,
                ).where(GameAccountRegistrationRequestORM.id == created.snapshot.request_id)
            ).one()
            actor_persona_id = connection.scalar(
                select(DiscordAccountORM.persona_id).where(
                    DiscordAccountORM.discord_user_id == command.actor_discord_user_id
                )
            )
            audit = connection.execute(
                select(
                    IdentityOperationORM.type,
                    IdentityOperationORM.persona_id,
                    IdentityOperationORM.game_account_id,
                )
                .join(OperationORM, OperationORM.id == IdentityOperationORM.operation_id)
                .where(OperationORM.idempotency_key == command.idempotency_key)
            ).one()
            assert actor_persona_id is None
            assert request.status == "pending"
            assert request.discord_display_name_snapshot == "MariaDB 요청자"
            assert request.affiliation == "통합 테스트"
            assert audit.type == "account_registration_requested"
            assert audit.persona_id is None
            assert audit.game_account_id is None
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(GameAccountORM)
                    .where(
                        GameAccountORM.game_region == command.game_region.value,
                        GameAccountORM.uma_pid == command.uma_pid,
                    )
                )
                == 0
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(DiscordPublicationORM)
                    .where(DiscordPublicationORM.guild_id == command.guild_id)
                )
                == 0
            )
    finally:
        _cleanup(migrated_engine, command=command)


def test_audit_failure_rolls_back_request_and_new_discord_actor(
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = str(uuid4().int % 10**17).zfill(17)
    command = _command(suffix=suffix)
    commands = compose_account_registration_commands(DatabaseRuntime.from_engine(migrated_engine))

    def fail_audit(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated registration audit failure")

    monkeypatch.setattr(SqlAlchemyAccountRegistrationRepository, "add_request_audit", fail_audit)
    with pytest.raises(RuntimeError, match="simulated registration audit failure"):
        commands.submit_registration_request(command)

    assert _count_for_actor(migrated_engine, DiscordAccountORM, command.actor_discord_user_id) == 0
    assert (
        _count_for_actor(
            migrated_engine,
            GameAccountRegistrationRequestORM,
            command.actor_discord_user_id,
        )
        == 0
    )
    assert _count_for_actor(migrated_engine, OperationORM, command.actor_discord_user_id) == 0


def test_concurrent_exact_request_converges_to_one_pending_fact_and_operation(
    migrated_engine: Engine,
) -> None:
    suffix = str(uuid4().int % 10**17).zfill(17)
    command = _command(suffix=suffix)
    barrier = Barrier(2)

    def submit() -> object:
        commands = compose_account_registration_commands(DatabaseRuntime.from_engine(migrated_engine))
        barrier.wait()
        return commands.submit_registration_request(command)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures: tuple[Future[object], ...] = tuple(executor.submit(submit) for _ in range(2))
            receipts = tuple(future.result(timeout=20) for future in futures)

        assert sorted(receipt.exact_retry for receipt in receipts) == [False, True]  # type: ignore[attr-defined]
        assert len({receipt.snapshot.request_id for receipt in receipts}) == 1  # type: ignore[attr-defined]
        assert (
            _count_for_actor(
                migrated_engine,
                GameAccountRegistrationRequestORM,
                command.actor_discord_user_id,
            )
            == 1
        )
        assert _count_for_actor(migrated_engine, OperationORM, command.actor_discord_user_id) == 1
    finally:
        _cleanup(migrated_engine, command=command)


def test_concurrent_different_keys_are_serialized_to_one_active_request(
    migrated_engine: Engine,
) -> None:
    suffix = str(uuid4().int % 10**17).zfill(17)
    first = _command(suffix=suffix, key=f"account-register-a-{suffix}")
    second = _command(suffix=suffix, key=f"account-register-b-{suffix}")
    barrier = Barrier(2)

    def submit(command: SubmitAccountRegistrationRequest) -> tuple[str, int]:
        commands = compose_account_registration_commands(DatabaseRuntime.from_engine(migrated_engine))
        barrier.wait()
        try:
            receipt = commands.submit_registration_request(command)
        except AccountRegistrationAlreadyPendingError as error:
            return "pending", error.request_id
        return "created", receipt.snapshot.request_id

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (executor.submit(submit, first), executor.submit(submit, second))
            outcomes = tuple(future.result(timeout=20) for future in futures)

        assert sorted(kind for kind, _request_id in outcomes) == ["created", "pending"]
        assert len({request_id for _kind, request_id in outcomes}) == 1
        assert (
            _count_for_actor(
                migrated_engine,
                GameAccountRegistrationRequestORM,
                first.actor_discord_user_id,
            )
            == 1
        )
        assert _count_for_actor(migrated_engine, OperationORM, first.actor_discord_user_id) == 1
    finally:
        _cleanup(migrated_engine, command=first)
