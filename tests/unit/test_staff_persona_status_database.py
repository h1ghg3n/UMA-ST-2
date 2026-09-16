"""SQLite persistence tests for staff Persona status management."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.identity import (
    ChangePersonaStatus,
    StaffPersonaStatusCommands,
    StaffPersonaStatusQueries,
    StaffPersonaStatusStaleError,
)
from uma_st2.domain.identity import PersonaStatus
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyStaffPersonaStatusQueryUnitOfWorkFactory,
    SqlAlchemyStaffPersonaStatusUnitOfWorkFactory,
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

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
STORED_NOW = NOW.replace(tzinfo=None)
PERSONA_ID = "11111111-2222-4333-8444-555555555555"


def _runtime() -> DatabaseRuntime:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    return runtime


def _seed(runtime: DatabaseRuntime) -> None:
    with runtime.session_factory.begin() as session:
        session.add_all(
            [
                PersonaORM(
                    id=PERSONA_ID,
                    display_name="상태 대상",
                    status="normal",
                    created_at=STORED_NOW,
                    updated_at=STORED_NOW,
                ),
                DiscordAccountORM(
                    id=1,
                    discord_user_id="123456",
                    persona_id=PERSONA_ID,
                    created_at=STORED_NOW,
                    updated_at=STORED_NOW,
                ),
                GameAccountORM(
                    id=1,
                    persona_id=PERSONA_ID,
                    game_region="KR",
                    uma_pid="123456789",
                    nickname="게임 계정",
                    affiliation="기존 소속",
                    created_at=STORED_NOW,
                    updated_at=STORED_NOW,
                ),
                CirclePointORM(persona_id=PERSONA_ID, balance=700, updated_at=STORED_NOW),
            ]
        )


def _queries(runtime: DatabaseRuntime) -> StaffPersonaStatusQueries:
    return StaffPersonaStatusQueries(
        QueryRunner(SqlAlchemyStaffPersonaStatusQueryUnitOfWorkFactory(runtime.session_factory))
    )


def _commands(runtime: DatabaseRuntime) -> StaffPersonaStatusCommands:
    return StaffPersonaStatusCommands(
        CommandRunner(SqlAlchemyStaffPersonaStatusUnitOfWorkFactory(runtime.session_factory)),
        clock=lambda: NOW,
    )


def _count(runtime: DatabaseRuntime, model: type[object]) -> int:
    with runtime.session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_status_change_updates_only_persona_status_and_audit_with_exact_retry() -> None:
    runtime = _runtime()
    try:
        _seed(runtime)
        preview = _queries(runtime).get_preview(
            guild_id="987",
            persona_id=PERSONA_ID,
            desired_status=PersonaStatus.PENDING_APPROVAL,
            reason="승인 자료 재검토",
        )
        command = ChangePersonaStatus(
            guild_id="987",
            target_persona_id=PERSONA_ID,
            desired_status=preview.desired_status,
            reason=preview.reason,
            updated_by_discord_user_id="900",
            expected_target_fingerprint=preview.state.state_fingerprint,
            idempotency_key="persona-status-1",
            correlation_id="persona-status-1",
        )

        result = _commands(runtime).change(command)
        retry = _commands(runtime).change(command)

        assert result.previous_status is PersonaStatus.NORMAL
        assert result.status is PersonaStatus.PENDING_APPROVAL
        assert retry.exact_retry is True
        with runtime.session_factory() as session:
            persona = session.get(PersonaORM, PERSONA_ID)
            link = session.get(DiscordAccountORM, 1)
            account = session.get(GameAccountORM, 1)
            wallet = session.get(CirclePointORM, PERSONA_ID)
            operation = session.scalar(select(OperationORM))
            audit = session.scalar(select(IdentityOperationORM))

            assert persona is not None and (persona.display_name, persona.status) == (
                "상태 대상",
                "pending_approval",
            )
            assert link is not None and link.persona_id == PERSONA_ID
            assert account is not None and (account.persona_id, account.uma_pid, account.affiliation) == (
                PERSONA_ID,
                "123456789",
                "기존 소속",
            )
            assert wallet is not None and wallet.balance == 700
            assert operation is not None and operation.reason == "승인 자료 재검토"
            assert audit is not None
            assert audit.type == "persona_status_changed"
            assert audit.game_account_id is None
            assert audit.before_data["status"] == "normal"
            assert audit.after_data["status"] == "pending_approval"

        assert _count(runtime, OperationORM) == 1
        assert _count(runtime, IdentityOperationORM) == 1
        assert _count(runtime, PointTransactionORM) == 0
        assert _count(runtime, GameAccountRegistrationRequestORM) == 0
        assert _count(runtime, DiscordPublicationORM) == 0
    finally:
        runtime.dispose()


def test_status_change_stale_preview_is_zero_write() -> None:
    runtime = _runtime()
    try:
        _seed(runtime)
        preview = _queries(runtime).get_preview(
            guild_id="987",
            persona_id=PERSONA_ID,
            desired_status=PersonaStatus.WARNING,
            reason="주의 상태",
        )
        with runtime.session_factory.begin() as session:
            persona = session.get(PersonaORM, PERSONA_ID)
            assert persona is not None
            persona.display_name = "변경된 이름"

        with pytest.raises(StaffPersonaStatusStaleError):
            _commands(runtime).change(
                ChangePersonaStatus(
                    guild_id="987",
                    target_persona_id=PERSONA_ID,
                    desired_status=preview.desired_status,
                    reason=preview.reason,
                    updated_by_discord_user_id="900",
                    expected_target_fingerprint=preview.state.state_fingerprint,
                    idempotency_key="persona-status-stale",
                )
            )

        with runtime.session_factory() as session:
            persona = session.get(PersonaORM, PERSONA_ID)
            assert persona is not None and persona.status == "normal"
        assert _count(runtime, OperationORM) == 0
        assert _count(runtime, IdentityOperationORM) == 0
    finally:
        runtime.dispose()


def test_status_audit_failure_rolls_back_status_and_operation() -> None:
    runtime = _runtime()
    try:
        _seed(runtime)
        preview = _queries(runtime).get_preview(
            guild_id="987",
            persona_id=PERSONA_ID,
            desired_status=PersonaStatus.WITHDRAWN,
            reason="탈퇴 확인",
        )
        command = ChangePersonaStatus(
            guild_id="987",
            target_persona_id=PERSONA_ID,
            desired_status=preview.desired_status,
            reason=preview.reason,
            updated_by_discord_user_id="900",
            expected_target_fingerprint=preview.state.state_fingerprint,
            idempotency_key="persona-status-fail",
        )

        def fail_audit(session: Session, _flush_context: object, _instances: object) -> None:
            if any(isinstance(item, IdentityOperationORM) for item in session.new):
                raise RuntimeError("simulated status audit failure")

        event.listen(Session, "before_flush", fail_audit)
        try:
            with pytest.raises(RuntimeError, match="simulated status audit failure"):
                _commands(runtime).change(command)
        finally:
            event.remove(Session, "before_flush", fail_audit)

        with runtime.session_factory() as session:
            persona = session.get(PersonaORM, PERSONA_ID)
            assert persona is not None and persona.status == "normal"
        assert _count(runtime, OperationORM) == 0
        assert _count(runtime, IdentityOperationORM) == 0
    finally:
        runtime.dispose()
