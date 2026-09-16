"""SQLite persistence tests for staff direct account registration."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.identity import (
    DirectlyRegisterDiscordAccount,
    StaffDirectRegistrationCommands,
    StaffDirectRegistrationPendingRequestError,
    StaffDirectRegistrationPidUnavailableError,
    StaffDirectRegistrationQueries,
    StaffDirectRegistrationTargetLinkedError,
)
from uma_st2.domain.identity import GameRegion
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyStaffDirectRegistrationQueryUnitOfWorkFactory,
    SqlAlchemyStaffDirectRegistrationUnitOfWorkFactory,
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

NOW = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)
STORED_NOW = NOW.replace(tzinfo=None)
PERSONA_ID = "11111111-2222-4333-8444-555555555555"


def _runtime() -> DatabaseRuntime:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    return runtime


def _queries(runtime: DatabaseRuntime) -> StaffDirectRegistrationQueries:
    return StaffDirectRegistrationQueries(
        QueryRunner(SqlAlchemyStaffDirectRegistrationQueryUnitOfWorkFactory(runtime.session_factory))
    )


def _commands(runtime: DatabaseRuntime) -> StaffDirectRegistrationCommands:
    return StaffDirectRegistrationCommands(
        CommandRunner(SqlAlchemyStaffDirectRegistrationUnitOfWorkFactory(runtime.session_factory)),
        clock=lambda: NOW,
        persona_id_factory=lambda: PERSONA_ID,
    )


def _command(
    runtime: DatabaseRuntime,
    *,
    target_user_id: str = "123456",
    uma_pid: str = "123456789",
    key: str = "direct-register-1",
) -> DirectlyRegisterDiscordAccount:
    preview = _queries(runtime).get_preview(
        guild_id="987",
        target_discord_user_id=target_user_id,
        target_display_name_snapshot="Discord 표시명",
        game_region=GameRegion.KR,
        uma_pid=uma_pid,
        nickname="게임 닉네임",
        affiliation="소속",
        operational_note="운영 확인 완료",
    )
    return DirectlyRegisterDiscordAccount(
        guild_id="987",
        target_discord_user_id=target_user_id,
        target_display_name_snapshot=preview.target_display_name_snapshot,
        game_region=preview.state.game_region,
        uma_pid=preview.state.uma_pid,
        nickname=preview.nickname,
        affiliation=preview.affiliation,
        registered_by_discord_user_id="900",
        expected_target_fingerprint=preview.state.state_fingerprint,
        idempotency_key=key,
        correlation_id=key,
        operational_note=preview.operational_note,
    )


def _count(runtime: DatabaseRuntime, model: type[object]) -> int:
    with runtime.session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_direct_registration_atomically_bootstraps_identity_wallet_grant_and_audit() -> None:
    runtime = _runtime()
    try:
        command = _command(runtime)
        commands = _commands(runtime)

        created = commands.register(command)
        retried = commands.register(command)

        assert created.persona_id == PERSONA_ID
        assert created.initial_grant_amount == created.wallet_balance == 500
        assert retried.exact_retry is True
        with runtime.session_factory() as session:
            persona = session.get(PersonaORM, PERSONA_ID)
            discord_account = session.scalar(
                select(DiscordAccountORM).where(DiscordAccountORM.discord_user_id == "123456")
            )
            game_account = session.scalar(select(GameAccountORM).where(GameAccountORM.persona_id == PERSONA_ID))
            wallet = session.get(CirclePointORM, PERSONA_ID)
            point = session.scalar(select(PointTransactionORM).where(PointTransactionORM.persona_id == PERSONA_ID))
            audit = session.scalar(
                select(IdentityOperationORM).where(IdentityOperationORM.type == "account_directly_registered")
            )
            operation = session.get(OperationORM, audit.operation_id if audit is not None else -1)

            assert persona is not None
            assert (persona.display_name, persona.status) == ("Discord 표시명", "normal")
            assert discord_account is not None and discord_account.persona_id == PERSONA_ID
            assert game_account is not None
            assert (
                game_account.game_region,
                game_account.uma_pid,
                game_account.nickname,
                game_account.affiliation,
            ) == ("KR", "123456789", "게임 닉네임", "소속")
            assert wallet is not None and wallet.balance == 500
            assert point is not None and (point.action, point.amount) == ("initial_grant", 500)
            assert audit is not None
            assert (audit.persona_id, audit.game_account_id, audit.discord_user_id) == (
                PERSONA_ID,
                game_account.id,
                "123456",
            )
            assert audit.before_data["active_registration_request_id"] is None
            assert audit.after_data["schema_version"] == 1
            assert operation is not None
            assert operation.actor_discord_user_id == "900"
            assert operation.reason == "운영 확인 완료"

        assert _count(runtime, PersonaORM) == 1
        assert _count(runtime, GameAccountORM) == 1
        assert _count(runtime, CirclePointORM) == 1
        assert _count(runtime, PointTransactionORM) == 1
        assert _count(runtime, OperationORM) == 1
        assert _count(runtime, IdentityOperationORM) == 1
        assert _count(runtime, GameAccountRegistrationRequestORM) == 0
        assert _count(runtime, DiscordPublicationORM) == 0
    finally:
        runtime.dispose()


def test_existing_link_pending_request_and_registered_pid_are_zero_write() -> None:
    runtime = _runtime()
    try:
        with runtime.session_factory.begin() as session:
            session.add(
                PersonaORM(
                    id="existing-persona",
                    display_name="기존",
                    status="normal",
                    created_at=STORED_NOW,
                    updated_at=STORED_NOW,
                )
            )
            session.add_all(
                [
                    DiscordAccountORM(
                        id=1,
                        discord_user_id="111111",
                        persona_id="existing-persona",
                        created_at=STORED_NOW,
                        updated_at=STORED_NOW,
                    ),
                    DiscordAccountORM(
                        id=2,
                        discord_user_id="222222",
                        persona_id=None,
                        created_at=STORED_NOW,
                        updated_at=STORED_NOW,
                    ),
                ]
            )
            session.add(
                GameAccountRegistrationRequestORM(
                    id=51,
                    guild_id="987",
                    requester_discord_user_id="222222",
                    discord_display_name_snapshot="등록 요청자",
                    game_region="KR",
                    uma_pid="987654321",
                    nickname="요청 계정",
                    affiliation=None,
                    status="pending",
                    active_marker=True,
                    reason=None,
                    created_at=STORED_NOW,
                    resolved_at=None,
                )
            )
            session.add(
                GameAccountORM(
                    id=1,
                    persona_id="existing-persona",
                    game_region="KR",
                    uma_pid="333333333",
                    nickname="기존 계정",
                    affiliation=None,
                    created_at=STORED_NOW,
                    updated_at=STORED_NOW,
                )
            )

        with pytest.raises(StaffDirectRegistrationTargetLinkedError):
            _queries(runtime).get_preview(
                guild_id="987",
                target_discord_user_id="111111",
                target_display_name_snapshot="연결됨",
                game_region=GameRegion.KR,
                uma_pid="111111111",
                nickname="신규",
                affiliation=None,
                operational_note=None,
            )
        with pytest.raises(StaffDirectRegistrationPendingRequestError):
            _queries(runtime).get_preview(
                guild_id="987",
                target_discord_user_id="222222",
                target_display_name_snapshot="요청 중",
                game_region=GameRegion.KR,
                uma_pid="222222222",
                nickname="신규",
                affiliation=None,
                operational_note=None,
            )
        with pytest.raises(StaffDirectRegistrationPidUnavailableError):
            _queries(runtime).get_preview(
                guild_id="987",
                target_discord_user_id="444444",
                target_display_name_snapshot="PID 중복",
                game_region=GameRegion.KR,
                uma_pid="333333333",
                nickname="신규",
                affiliation=None,
                operational_note=None,
            )

        assert _count(runtime, PersonaORM) == 1
        assert _count(runtime, OperationORM) == 0
        assert _count(runtime, PointTransactionORM) == 0
    finally:
        runtime.dispose()


def test_audit_failure_rolls_back_complete_direct_registration_bootstrap() -> None:
    runtime = _runtime()
    try:
        command = _command(runtime, target_user_id="555555", key="direct-register-fail")

        def fail_audit(session: Session, _flush_context: object, _instances: object) -> None:
            if any(isinstance(item, IdentityOperationORM) for item in session.new):
                raise RuntimeError("simulated direct-registration audit failure")

        event.listen(Session, "before_flush", fail_audit)
        try:
            with pytest.raises(RuntimeError, match="simulated direct-registration audit failure"):
                _commands(runtime).register(command)
        finally:
            event.remove(Session, "before_flush", fail_audit)

        with runtime.session_factory() as session:
            target = session.scalar(select(DiscordAccountORM).where(DiscordAccountORM.discord_user_id == "555555"))
            assert target is None
        assert _count(runtime, PersonaORM) == 0
        assert _count(runtime, GameAccountORM) == 0
        assert _count(runtime, CirclePointORM) == 0
        assert _count(runtime, PointTransactionORM) == 0
        assert _count(runtime, OperationORM) == 0
        assert _count(runtime, IdentityOperationORM) == 0
    finally:
        runtime.dispose()
