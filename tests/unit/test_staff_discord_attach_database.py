"""SQLite persistence tests for staff direct Discord access attachment."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.identity import (
    AttachDiscordAccountToPersona,
    StaffDiscordAttachCommands,
    StaffDiscordAttachPendingRequestError,
    StaffDiscordAttachQueries,
    StaffDiscordAttachTargetLinkedError,
)
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyStaffDiscordAttachQueryUnitOfWorkFactory,
    SqlAlchemyStaffDiscordAttachUnitOfWorkFactory,
)
from uma_st2.infrastructure.database.orm import (
    Base,
    CirclePointORM,
    DiscordAccountORM,
    DiscordPublicationORM,
    GameAccountORM,
    GameAccountRegistrationRequestORM,
    IdentityOperationORM,
    MatchEntryORM,
    OperationORM,
    PersonaORM,
    PointTransactionORM,
    RatingORM,
)

NOW = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
STORED_NOW = NOW.replace(tzinfo=None)
PERSONA_ID = "11111111-2222-4333-8444-555555555555"


def _runtime() -> DatabaseRuntime:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    return runtime


def _seed_persona(runtime: DatabaseRuntime, *, with_wallet: bool = True) -> None:
    with runtime.session_factory.begin() as session:
        session.add(
            PersonaORM(
                id=PERSONA_ID,
                display_name="기존 Persona",
                status="normal",
                created_at=STORED_NOW,
                updated_at=STORED_NOW,
            )
        )
        session.add(
            GameAccountORM(
                id=1,
                persona_id=PERSONA_ID,
                game_region="KR",
                uma_pid="123456789",
                nickname="기존 계정",
                affiliation="소속",
                created_at=STORED_NOW,
                updated_at=STORED_NOW,
            )
        )
        if with_wallet:
            session.add(CirclePointORM(persona_id=PERSONA_ID, balance=700, updated_at=STORED_NOW))


def _queries(runtime: DatabaseRuntime) -> StaffDiscordAttachQueries:
    return StaffDiscordAttachQueries(
        QueryRunner(SqlAlchemyStaffDiscordAttachQueryUnitOfWorkFactory(runtime.session_factory))
    )


def _commands(runtime: DatabaseRuntime) -> StaffDiscordAttachCommands:
    return StaffDiscordAttachCommands(
        CommandRunner(SqlAlchemyStaffDiscordAttachUnitOfWorkFactory(runtime.session_factory)),
        clock=lambda: NOW,
    )


def _command(runtime: DatabaseRuntime, *, target_user_id: str = "123456", key: str = "attach-1"):
    preview = _queries(runtime).get_preview(
        guild_id="987",
        persona_id=PERSONA_ID,
        discord_user_id=target_user_id,
    )
    return AttachDiscordAccountToPersona(
        guild_id="987",
        target_persona_id=PERSONA_ID,
        target_discord_user_id=target_user_id,
        attached_by_discord_user_id="900",
        expected_target_fingerprint=preview.state_fingerprint,
        idempotency_key=key,
        correlation_id=key,
        operational_note="기존 Persona access 연결",
    )


def _count(runtime: DatabaseRuntime, model: type[object]) -> int:
    with runtime.session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_attach_creates_only_access_link_and_identity_audit_with_exact_retry() -> None:
    runtime = _runtime()
    try:
        _seed_persona(runtime)
        command = _command(runtime)
        commands = _commands(runtime)

        created = commands.attach(command)
        retried = commands.attach(command)

        assert created.member_mutation_eligible is True
        assert retried.exact_retry is True
        with runtime.session_factory() as session:
            persona = session.get(PersonaORM, PERSONA_ID)
            account = session.scalar(select(DiscordAccountORM).where(DiscordAccountORM.discord_user_id == "123456"))
            identity_operation = session.scalar(
                select(IdentityOperationORM).where(IdentityOperationORM.type == "discord_account_attached")
            )
            operation = session.get(
                OperationORM,
                identity_operation.operation_id if identity_operation is not None else -1,
            )
            wallet = session.get(CirclePointORM, PERSONA_ID)
            assert persona is not None and persona.status == "normal"
            assert account is not None and account.persona_id == PERSONA_ID
            assert wallet is not None and wallet.balance == 700
            assert identity_operation is not None
            assert (identity_operation.persona_id, identity_operation.game_account_id) == (
                PERSONA_ID,
                None,
            )
            assert identity_operation.discord_user_id == "123456"
            assert identity_operation.before_data["source"] == "discord_staff"
            assert operation is not None
            assert operation.actor_discord_user_id == "900"
            assert operation.reason == "기존 Persona access 연결"

        assert _count(runtime, PersonaORM) == 1
        assert _count(runtime, GameAccountORM) == 1
        assert _count(runtime, CirclePointORM) == 1
        assert _count(runtime, PointTransactionORM) == 0
        assert _count(runtime, RatingORM) == 0
        assert _count(runtime, MatchEntryORM) == 0
        assert _count(runtime, DiscordPublicationORM) == 0
        assert _count(runtime, OperationORM) == 1
        assert _count(runtime, IdentityOperationORM) == 1
    finally:
        runtime.dispose()


def test_attach_without_wallet_preserves_missing_wallet_and_reports_ineligible() -> None:
    runtime = _runtime()
    try:
        _seed_persona(runtime, with_wallet=False)

        result = _commands(runtime).attach(_command(runtime))

        assert result.has_wallet is False
        assert result.member_mutation_eligible is False
        assert _count(runtime, CirclePointORM) == 0
        assert _count(runtime, PointTransactionORM) == 0
    finally:
        runtime.dispose()


def test_existing_link_and_pending_request_are_zero_write() -> None:
    runtime = _runtime()
    try:
        _seed_persona(runtime)
        with runtime.session_factory.begin() as session:
            session.add(
                DiscordAccountORM(
                    id=1,
                    discord_user_id="111111",
                    persona_id=PERSONA_ID,
                    created_at=STORED_NOW,
                    updated_at=STORED_NOW,
                )
            )
            session.add(
                DiscordAccountORM(
                    id=2,
                    discord_user_id="222222",
                    persona_id=None,
                    created_at=STORED_NOW,
                    updated_at=STORED_NOW,
                )
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

        with pytest.raises(StaffDiscordAttachTargetLinkedError):
            _queries(runtime).get_preview(
                guild_id="987",
                persona_id=PERSONA_ID,
                discord_user_id="111111",
            )
        with pytest.raises(StaffDiscordAttachPendingRequestError):
            _queries(runtime).get_preview(
                guild_id="987",
                persona_id=PERSONA_ID,
                discord_user_id="222222",
            )

        assert _count(runtime, OperationORM) == 0
        assert _count(runtime, PointTransactionORM) == 0
        with runtime.session_factory() as session:
            pending = session.get(GameAccountRegistrationRequestORM, 51)
            assert pending is not None and pending.status == "pending" and pending.active_marker is True
    finally:
        runtime.dispose()


def test_audit_failure_rolls_back_new_discord_account_and_link() -> None:
    runtime = _runtime()
    try:
        _seed_persona(runtime)
        command = _command(runtime, target_user_id="333333", key="attach-fail")

        def fail_audit(session: Session, _flush_context: object, _instances: object) -> None:
            if any(isinstance(item, IdentityOperationORM) for item in session.new):
                raise RuntimeError("simulated attach audit failure")

        event.listen(Session, "before_flush", fail_audit)
        try:
            with pytest.raises(RuntimeError, match="simulated attach audit failure"):
                _commands(runtime).attach(command)
        finally:
            event.remove(Session, "before_flush", fail_audit)

        with runtime.session_factory() as session:
            account = session.scalar(select(DiscordAccountORM).where(DiscordAccountORM.discord_user_id == "333333"))
            assert account is None
        assert _count(runtime, OperationORM) == 0
        assert _count(runtime, IdentityOperationORM) == 0
        assert _count(runtime, PointTransactionORM) == 0
    finally:
        runtime.dispose()
