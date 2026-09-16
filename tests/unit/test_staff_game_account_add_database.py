"""SQLite persistence tests for staff peer GameAccount addition."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.identity import (
    AddGameAccountToPersona,
    StaffGameAccountAddCommands,
    StaffGameAccountAddPersonaRestrictedError,
    StaffGameAccountAddPidUnavailableError,
    StaffGameAccountAddQueries,
)
from uma_st2.domain.identity import GameRegion
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyStaffGameAccountAddQueryUnitOfWorkFactory,
    SqlAlchemyStaffGameAccountAddUnitOfWorkFactory,
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

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
STORED_NOW = NOW.replace(tzinfo=None)
PERSONA_ID = "11111111-2222-4333-8444-555555555555"


def _runtime() -> DatabaseRuntime:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    return runtime


def _seed(runtime: DatabaseRuntime, *, status: str = "normal", with_wallet: bool = True) -> None:
    with runtime.session_factory.begin() as session:
        session.add(
            PersonaORM(
                id=PERSONA_ID,
                display_name="기존 Persona",
                status=status,
                created_at=STORED_NOW,
                updated_at=STORED_NOW,
            )
        )
        session.add(
            GameAccountORM(
                id=1,
                persona_id=PERSONA_ID,
                game_region="KR",
                uma_pid="111111111",
                nickname="기존 계정",
                affiliation="기존 소속",
                created_at=STORED_NOW,
                updated_at=STORED_NOW,
            )
        )
        if with_wallet:
            session.add(
                CirclePointORM(
                    persona_id=PERSONA_ID,
                    balance=700,
                    updated_at=STORED_NOW,
                )
            )
        session.add(
            DiscordAccountORM(
                id=1,
                discord_user_id="123456",
                persona_id=PERSONA_ID,
                created_at=STORED_NOW,
                updated_at=STORED_NOW,
            )
        )


def _queries(runtime: DatabaseRuntime) -> StaffGameAccountAddQueries:
    return StaffGameAccountAddQueries(
        QueryRunner(SqlAlchemyStaffGameAccountAddQueryUnitOfWorkFactory(runtime.session_factory))
    )


def _commands(runtime: DatabaseRuntime) -> StaffGameAccountAddCommands:
    return StaffGameAccountAddCommands(
        CommandRunner(SqlAlchemyStaffGameAccountAddUnitOfWorkFactory(runtime.session_factory)),
        clock=lambda: NOW,
    )


def _command(
    runtime: DatabaseRuntime,
    *,
    uma_pid: str = "222222222",
    key: str = "game-account-add-1",
) -> AddGameAccountToPersona:
    preview = _queries(runtime).get_preview(
        guild_id="987",
        persona_id=PERSONA_ID,
        game_region=GameRegion.JP,
        uma_pid=uma_pid,
        nickname="새 계정",
        affiliation="새 소속",
        reason="복수 계정 확인",
    )
    return AddGameAccountToPersona(
        guild_id="987",
        target_persona_id=PERSONA_ID,
        game_region=preview.state.game_region,
        uma_pid=preview.state.uma_pid,
        nickname=preview.nickname,
        affiliation=preview.affiliation,
        reason=preview.reason,
        added_by_discord_user_id="900",
        expected_target_fingerprint=preview.state.state_fingerprint,
        idempotency_key=key,
        correlation_id=key,
    )


def _count(runtime: DatabaseRuntime, model: type[object]) -> int:
    with runtime.session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_peer_add_atomically_writes_only_account_and_identity_audit() -> None:
    runtime = _runtime()
    try:
        _seed(runtime)
        command = _command(runtime)
        commands = _commands(runtime)

        created = commands.add(command)
        retried = commands.add(command)

        assert created.game_account_count == created.qualifying_game_account_count == 2
        assert created.member_mutation_eligible is True
        assert retried.exact_retry is True
        with runtime.session_factory() as session:
            account = session.get(GameAccountORM, created.game_account_id)
            wallet = session.get(CirclePointORM, PERSONA_ID)
            link = session.scalar(select(DiscordAccountORM).where(DiscordAccountORM.discord_user_id == "123456"))
            audit = session.scalar(
                select(IdentityOperationORM).where(IdentityOperationORM.type == "game_account_added")
            )
            operation = session.get(OperationORM, audit.operation_id if audit is not None else -1)

            assert account is not None
            assert (account.persona_id, account.game_region, account.uma_pid) == (PERSONA_ID, "JP", "222222222")
            assert (account.nickname, account.affiliation) == ("새 계정", "새 소속")
            assert wallet is not None and wallet.balance == 700
            assert link is not None and link.persona_id == PERSONA_ID
            assert audit is not None
            assert (audit.persona_id, audit.game_account_id, audit.discord_user_id) == (
                PERSONA_ID,
                created.game_account_id,
                None,
            )
            assert audit.before_data["game_account_count"] == 1
            assert audit.after_data["game_account_count"] == 2
            assert operation is not None
            assert (operation.actor_discord_user_id, operation.reason) == ("900", "복수 계정 확인")

        assert _count(runtime, GameAccountORM) == 2
        assert _count(runtime, OperationORM) == 1
        assert _count(runtime, IdentityOperationORM) == 1
        assert _count(runtime, PointTransactionORM) == 0
        assert _count(runtime, GameAccountRegistrationRequestORM) == 0
        assert _count(runtime, DiscordPublicationORM) == 0
    finally:
        runtime.dispose()


def test_existing_pid_and_terminal_persona_are_zero_write() -> None:
    runtime = _runtime()
    try:
        _seed(runtime, status="expelled")
        with pytest.raises(StaffGameAccountAddPersonaRestrictedError):
            _queries(runtime).get_preview(
                guild_id="987",
                persona_id=PERSONA_ID,
                game_region=GameRegion.JP,
                uma_pid="222222222",
                nickname="새 계정",
                affiliation=None,
                reason="확인",
            )

        with runtime.session_factory.begin() as session:
            persona = session.get(PersonaORM, PERSONA_ID)
            assert persona is not None
            persona.status = "normal"

        with pytest.raises(StaffGameAccountAddPidUnavailableError):
            _queries(runtime).get_preview(
                guild_id="987",
                persona_id=PERSONA_ID,
                game_region=GameRegion.KR,
                uma_pid="111111111",
                nickname="중복",
                affiliation=None,
                reason="확인",
            )

        assert _count(runtime, GameAccountORM) == 1
        assert _count(runtime, OperationORM) == 0
        assert _count(runtime, PointTransactionORM) == 0
    finally:
        runtime.dispose()


def test_pending_persona_without_wallet_gains_account_without_wallet_repair() -> None:
    runtime = _runtime()
    try:
        _seed(runtime, status="pending_approval", with_wallet=False)

        result = _commands(runtime).add(_command(runtime, key="game-account-add-pending"))

        assert result.persona_status.value == "pending_approval"
        assert result.has_wallet is False
        assert result.member_mutation_eligible is False
        assert _count(runtime, GameAccountORM) == 2
        assert _count(runtime, CirclePointORM) == 0
        assert _count(runtime, PointTransactionORM) == 0
    finally:
        runtime.dispose()


def test_audit_failure_rolls_back_peer_account_and_operation() -> None:
    runtime = _runtime()
    try:
        _seed(runtime)
        command = _command(runtime, key="game-account-add-fail")

        def fail_audit(session: Session, _flush_context: object, _instances: object) -> None:
            if any(isinstance(item, IdentityOperationORM) for item in session.new):
                raise RuntimeError("simulated peer-add audit failure")

        event.listen(Session, "before_flush", fail_audit)
        try:
            with pytest.raises(RuntimeError, match="simulated peer-add audit failure"):
                _commands(runtime).add(command)
        finally:
            event.remove(Session, "before_flush", fail_audit)

        assert _count(runtime, GameAccountORM) == 1
        assert _count(runtime, OperationORM) == 0
        assert _count(runtime, IdentityOperationORM) == 0
        with runtime.session_factory() as session:
            wallet = session.get(CirclePointORM, PERSONA_ID)
            assert wallet is not None and wallet.balance == 700
    finally:
        runtime.dispose()
