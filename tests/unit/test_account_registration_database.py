"""Focused SQLAlchemy persistence tests for Account registration requests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select

from uma_st2.application.execution import CommandRunner
from uma_st2.application.identity import (
    AccountRegistrationCommands,
    AccountRegistrationPidUnavailableError,
    SubmitAccountRegistrationRequest,
)
from uma_st2.domain.identity import GameRegion
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyAccountRegistrationUnitOfWorkFactory,
)
from uma_st2.infrastructure.database.orm import (
    Base,
    CirclePointORM,
    DiscordAccountORM,
    DiscordPublicationORM,
    GameAccountORM,
    GameAccountRegistrationRequestORM,
    IdentityOperationORM,
    OperationORM,
    PersonaORM,
    PointTransactionORM,
)

NOW = datetime(2026, 9, 2, 5, 0, tzinfo=UTC)


def _runtime() -> DatabaseRuntime:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    return runtime


def _commands(runtime: DatabaseRuntime) -> AccountRegistrationCommands:
    return AccountRegistrationCommands(
        CommandRunner(SqlAlchemyAccountRegistrationUnitOfWorkFactory(runtime.session_factory)),
        clock=lambda: NOW,
    )


def _command(**changes: object) -> SubmitAccountRegistrationRequest:
    values: dict[str, object] = {
        "guild_id": "987",
        "actor_discord_user_id": "123",
        "discord_display_name_snapshot": "Discord 표시명",
        "game_region": GameRegion.KR,
        "uma_pid": "123456789",
        "nickname": "게임 닉네임",
        "affiliation": "소속",
        "idempotency_key": "555",
        "correlation_id": "555",
    }
    values.update(changes)
    return SubmitAccountRegistrationRequest(**values)  # type: ignore[arg-type]


def _count(runtime: DatabaseRuntime, model: type[object]) -> int:
    with runtime.session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_request_persists_only_unlinked_actor_pending_fact_and_identity_audit() -> None:
    runtime = _runtime()
    try:
        commands = _commands(runtime)
        created = commands.submit_registration_request(_command())
        retried = commands.submit_registration_request(_command())

        assert created.exact_retry is False
        assert retried.exact_retry is True
        assert retried.snapshot == created.snapshot
        with runtime.session_factory() as session:
            actor = session.scalar(select(DiscordAccountORM))
            request = session.scalar(select(GameAccountRegistrationRequestORM))
            operation = session.scalar(select(OperationORM))
            audit = session.scalar(select(IdentityOperationORM))
            assert actor is not None and actor.persona_id is None
            assert request is not None
            assert request.discord_display_name_snapshot == "Discord 표시명"
            assert request.affiliation == "소속"
            assert request.status == "pending"
            assert request.active_marker is True
            assert operation is not None and operation.idempotency_key == "555"
            assert audit is not None and audit.type == "account_registration_requested"
            assert audit.persona_id is None
            assert audit.game_account_id is None

        assert _count(runtime, DiscordAccountORM) == 1
        assert _count(runtime, GameAccountRegistrationRequestORM) == 1
        assert _count(runtime, OperationORM) == 1
        assert _count(runtime, IdentityOperationORM) == 1
        assert _count(runtime, PersonaORM) == 0
        assert _count(runtime, GameAccountORM) == 0
        assert _count(runtime, CirclePointORM) == 0
        assert _count(runtime, PointTransactionORM) == 0
        assert _count(runtime, DiscordPublicationORM) == 0
    finally:
        runtime.dispose()


def test_registered_pid_rejection_rolls_back_new_unlinked_actor() -> None:
    runtime = _runtime()
    try:
        with runtime.session_factory.begin() as session:
            session.add(
                PersonaORM(
                    id="persona-1",
                    display_name="기존 사용자",
                    status="normal",
                    created_at=NOW.replace(tzinfo=None),
                    updated_at=NOW.replace(tzinfo=None),
                )
            )
            session.add(
                GameAccountORM(
                    id=1,
                    persona_id="persona-1",
                    game_region="KR",
                    uma_pid="123456789",
                    nickname="기존 계정",
                    affiliation=None,
                    created_at=NOW.replace(tzinfo=None),
                    updated_at=NOW.replace(tzinfo=None),
                )
            )

        with pytest.raises(AccountRegistrationPidUnavailableError):
            _commands(runtime).submit_registration_request(_command())

        assert _count(runtime, DiscordAccountORM) == 0
        assert _count(runtime, GameAccountRegistrationRequestORM) == 0
        assert _count(runtime, OperationORM) == 0
    finally:
        runtime.dispose()
