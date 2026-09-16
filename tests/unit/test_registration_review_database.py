"""SQLite persistence tests for staff registration review and bootstrap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.identity import (
    AccountRegistrationCommands,
    ApproveAccountRegistrationRequest,
    RegistrationReviewCommands,
    RejectAccountRegistrationRequest,
    StaffRegistrationPidUnavailableError,
    StaffRegistrationRequesterLinkedError,
    StaffRegistrationReviewQueries,
    SubmitAccountRegistrationRequest,
)
from uma_st2.domain.identity import GameRegion
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyAccountRegistrationUnitOfWorkFactory,
    SqlAlchemyRegistrationReviewUnitOfWorkFactory,
    SqlAlchemyStaffRegistrationReviewQueryUnitOfWorkFactory,
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

SUBMITTED_AT = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)
REVIEWED_AT = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)
PERSONA_ID = "11111111-2222-4333-8444-555555555555"


def _runtime() -> DatabaseRuntime:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    return runtime


def _submission_commands(runtime: DatabaseRuntime) -> AccountRegistrationCommands:
    return AccountRegistrationCommands(
        CommandRunner(SqlAlchemyAccountRegistrationUnitOfWorkFactory(runtime.session_factory)),
        clock=lambda: SUBMITTED_AT,
    )


def _review_commands(runtime: DatabaseRuntime) -> RegistrationReviewCommands:
    return RegistrationReviewCommands(
        CommandRunner(SqlAlchemyRegistrationReviewUnitOfWorkFactory(runtime.session_factory)),
        clock=lambda: REVIEWED_AT,
        persona_id_factory=lambda: PERSONA_ID,
    )


def _queries(runtime: DatabaseRuntime) -> StaffRegistrationReviewQueries:
    return StaffRegistrationReviewQueries(
        QueryRunner(SqlAlchemyStaffRegistrationReviewQueryUnitOfWorkFactory(runtime.session_factory))
    )


def _submit(runtime: DatabaseRuntime, **changes: object) -> int:
    values: dict[str, object] = {
        "guild_id": "987",
        "actor_discord_user_id": "123",
        "discord_display_name_snapshot": "Discord 표시명",
        "game_region": GameRegion.KR,
        "uma_pid": "123456789",
        "nickname": "게임 닉네임",
        "affiliation": "소속",
        "idempotency_key": "submit-1",
        "correlation_id": "submit-1",
    }
    values.update(changes)
    result = _submission_commands(runtime).submit_registration_request(
        SubmitAccountRegistrationRequest(**values)  # type: ignore[arg-type]
    )
    return result.snapshot.request_id


def _approve(runtime: DatabaseRuntime, *, request_id: int, key: str = "approve-1"):
    detail = _queries(runtime).get_pending_request(guild_id="987", request_id=request_id)
    return _review_commands(runtime).approve(
        ApproveAccountRegistrationRequest(
            request_id=request_id,
            guild_id="987",
            reviewed_by_discord_user_id="900",
            expected_request_fingerprint=detail.state_fingerprint,
            idempotency_key=key,
            correlation_id=key,
        )
    )


def _count(runtime: DatabaseRuntime, model: type[object]) -> int:
    with runtime.session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_approval_atomically_bootstraps_identity_wallet_grant_request_and_audit() -> None:
    runtime = _runtime()
    try:
        request_id = _submit(runtime)
        queries = _queries(runtime)
        panel = queries.get_panel(guild_id="987")
        page = queries.list_pending_requests(guild_id="987", page=0)
        detail = queries.get_pending_request(guild_id="987", request_id=request_id)
        assert panel.pending_request_count == 1
        assert page.items == (detail,)

        command = ApproveAccountRegistrationRequest(
            request_id=request_id,
            guild_id="987",
            reviewed_by_discord_user_id="900",
            expected_request_fingerprint=detail.state_fingerprint,
            idempotency_key="approve-1",
            correlation_id="approve-1",
        )
        commands = _review_commands(runtime)
        created = commands.approve(command)
        retried = commands.approve(command)

        assert created.persona_id == PERSONA_ID
        assert created.persona_display_name == "Discord 표시명"
        assert created.initial_grant_amount == created.wallet_balance == 500
        assert retried.exact_retry is True
        with runtime.session_factory() as session:
            persona = session.get(PersonaORM, PERSONA_ID)
            actor = session.scalar(select(DiscordAccountORM).where(DiscordAccountORM.discord_user_id == "123"))
            game = session.scalar(select(GameAccountORM).where(GameAccountORM.persona_id == PERSONA_ID))
            wallet = session.get(CirclePointORM, PERSONA_ID)
            point = session.scalar(select(PointTransactionORM).where(PointTransactionORM.persona_id == PERSONA_ID))
            request = session.get(GameAccountRegistrationRequestORM, request_id)
            approval = session.scalar(
                select(IdentityOperationORM).where(IdentityOperationORM.type == "account_registration_approved")
            )
            operation = session.get(OperationORM, approval.operation_id if approval is not None else -1)
            assert persona is not None and persona.status == "normal"
            assert actor is not None and actor.persona_id == PERSONA_ID
            assert game is not None
            assert (game.game_region, game.uma_pid, game.nickname, game.affiliation) == (
                "KR",
                "123456789",
                "게임 닉네임",
                "소속",
            )
            assert wallet is not None and wallet.balance == 500
            assert point is not None
            assert (point.action, point.amount, point.operation_id) == (
                "initial_grant",
                500,
                approval.operation_id if approval is not None else None,
            )
            assert request is not None
            assert (request.status, request.active_marker, request.resolved_at) == (
                "approved",
                None,
                REVIEWED_AT.replace(tzinfo=None),
            )
            assert approval is not None
            assert (approval.persona_id, approval.game_account_id, approval.discord_user_id) == (
                PERSONA_ID,
                game.id,
                "123",
            )
            assert operation is not None
            assert operation.actor_discord_user_id == "900"
            assert operation.idempotency_key == "approve-1"

        assert _count(runtime, PersonaORM) == 1
        assert _count(runtime, GameAccountORM) == 1
        assert _count(runtime, CirclePointORM) == 1
        assert _count(runtime, PointTransactionORM) == 1
        assert _count(runtime, OperationORM) == 2
        assert _count(runtime, IdentityOperationORM) == 2
        assert _count(runtime, DiscordPublicationORM) == 0
        assert queries.get_panel(guild_id="987").pending_request_count == 0
    finally:
        runtime.dispose()


def test_rejection_resolves_request_and_audit_without_identity_or_economy() -> None:
    runtime = _runtime()
    try:
        request_id = _submit(runtime)
        detail = _queries(runtime).get_pending_request(guild_id="987", request_id=request_id)
        command = RejectAccountRegistrationRequest(
            request_id=request_id,
            guild_id="987",
            reviewed_by_discord_user_id="900",
            expected_request_fingerprint=detail.state_fingerprint,
            reason="PID 확인 불가",
            idempotency_key="reject-1",
            correlation_id="reject-1",
        )
        commands = _review_commands(runtime)
        created = commands.reject(command)
        retried = commands.reject(command)

        assert created.reason == "PID 확인 불가"
        assert retried.exact_retry is True
        with runtime.session_factory() as session:
            request = session.get(GameAccountRegistrationRequestORM, request_id)
            audit = session.scalar(
                select(IdentityOperationORM).where(IdentityOperationORM.type == "account_registration_rejected")
            )
            assert request is not None
            assert (request.status, request.active_marker, request.reason) == (
                "cancelled",
                None,
                "PID 확인 불가",
            )
            assert audit is not None
            assert audit.persona_id is audit.game_account_id is None
            assert audit.discord_user_id == "123"
        assert _count(runtime, PersonaORM) == 0
        assert _count(runtime, GameAccountORM) == 0
        assert _count(runtime, CirclePointORM) == 0
        assert _count(runtime, PointTransactionORM) == 0
        assert _count(runtime, DiscordPublicationORM) == 0
    finally:
        runtime.dispose()


def test_approval_fails_closed_when_requester_link_or_pid_changes_after_preview() -> None:
    runtime = _runtime()
    try:
        request_id = _submit(runtime)
        detail = _queries(runtime).get_pending_request(guild_id="987", request_id=request_id)
        with runtime.session_factory.begin() as session:
            session.add(
                PersonaORM(
                    id="existing-persona",
                    display_name="기존",
                    status="normal",
                    created_at=SUBMITTED_AT.replace(tzinfo=None),
                    updated_at=SUBMITTED_AT.replace(tzinfo=None),
                )
            )
            actor = session.scalar(select(DiscordAccountORM).where(DiscordAccountORM.discord_user_id == "123"))
            assert actor is not None
            actor.persona_id = "existing-persona"

        command = ApproveAccountRegistrationRequest(
            request_id=request_id,
            guild_id="987",
            reviewed_by_discord_user_id="900",
            expected_request_fingerprint=detail.state_fingerprint,
            idempotency_key="approve-linked",
        )
        with pytest.raises(StaffRegistrationRequesterLinkedError):
            _review_commands(runtime).approve(command)

        with runtime.session_factory.begin() as session:
            actor = session.scalar(select(DiscordAccountORM).where(DiscordAccountORM.discord_user_id == "123"))
            assert actor is not None
            actor.persona_id = None
            session.add(
                GameAccountORM(
                    id=1,
                    persona_id="existing-persona",
                    game_region="KR",
                    uma_pid="123456789",
                    nickname="선점 계정",
                    affiliation=None,
                    created_at=SUBMITTED_AT.replace(tzinfo=None),
                    updated_at=SUBMITTED_AT.replace(tzinfo=None),
                )
            )
        with pytest.raises(StaffRegistrationPidUnavailableError):
            _review_commands(runtime).approve(command)

        with runtime.session_factory() as session:
            request = session.get(GameAccountRegistrationRequestORM, request_id)
            actor = session.scalar(select(DiscordAccountORM).where(DiscordAccountORM.discord_user_id == "123"))
            assert request is not None and request.status == "pending" and request.active_marker is True
            assert actor is not None and actor.persona_id is None
        assert _count(runtime, PointTransactionORM) == 0
    finally:
        runtime.dispose()


def test_initial_grant_flush_failure_rolls_back_complete_approval_bootstrap() -> None:
    runtime = _runtime()
    try:
        request_id = _submit(runtime)
        detail = _queries(runtime).get_pending_request(guild_id="987", request_id=request_id)
        command = ApproveAccountRegistrationRequest(
            request_id=request_id,
            guild_id="987",
            reviewed_by_discord_user_id="900",
            expected_request_fingerprint=detail.state_fingerprint,
            idempotency_key="approve-fail",
        )

        def fail_grant(session: Session, _flush_context: object, _instances: object) -> None:
            if any(isinstance(item, PointTransactionORM) for item in session.new):
                raise RuntimeError("simulated initial grant failure")

        event.listen(Session, "before_flush", fail_grant)
        try:
            with pytest.raises(RuntimeError, match="simulated initial grant failure"):
                _review_commands(runtime).approve(command)
        finally:
            event.remove(Session, "before_flush", fail_grant)

        with runtime.session_factory() as session:
            request = session.get(GameAccountRegistrationRequestORM, request_id)
            actor = session.scalar(select(DiscordAccountORM).where(DiscordAccountORM.discord_user_id == "123"))
            assert request is not None and request.status == "pending" and request.active_marker is True
            assert actor is not None and actor.persona_id is None
        assert _count(runtime, PersonaORM) == 0
        assert _count(runtime, GameAccountORM) == 0
        assert _count(runtime, CirclePointORM) == 0
        assert _count(runtime, PointTransactionORM) == 0
        assert _count(runtime, OperationORM) == 1
        assert _count(runtime, IdentityOperationORM) == 1
    finally:
        runtime.dispose()


def test_pending_request_query_pages_ten_rows_and_persona_context_is_not_merge_authority() -> None:
    runtime = _runtime()
    try:
        with runtime.session_factory.begin() as session:
            session.add(
                PersonaORM(
                    id="context-persona",
                    display_name="선택 Persona",
                    status="warning",
                    created_at=SUBMITTED_AT.replace(tzinfo=None),
                    updated_at=SUBMITTED_AT.replace(tzinfo=None),
                )
            )
            for index in range(11):
                actor_id = 1000 + index
                session.add(
                    DiscordAccountORM(
                        id=actor_id,
                        discord_user_id=str(actor_id),
                        persona_id=None,
                        created_at=SUBMITTED_AT.replace(tzinfo=None),
                        updated_at=SUBMITTED_AT.replace(tzinfo=None),
                    )
                )
                session.add(
                    GameAccountRegistrationRequestORM(
                        id=actor_id,
                        guild_id="987",
                        requester_discord_user_id=str(actor_id),
                        discord_display_name_snapshot=f"요청자 {index}",
                        game_region="KR",
                        uma_pid=str(900000000 + index),
                        nickname=f"계정 {index}",
                        affiliation=None,
                        status="pending",
                        active_marker=True,
                        reason=None,
                        created_at=(SUBMITTED_AT + timedelta(minutes=index)).replace(tzinfo=None),
                        resolved_at=None,
                    )
                )

        queries = _queries(runtime)
        panel = queries.get_panel(guild_id="987", persona_id="context-persona")
        first = queries.list_pending_requests(guild_id="987", page=0)
        second = queries.list_pending_requests(guild_id="987", page=1)

        assert panel.pending_request_count == 11
        assert panel.selected_persona is not None
        assert panel.selected_persona.display_name == "선택 Persona"
        assert len(first.items) == 10 and first.has_next
        assert len(second.items) == 1 and second.has_previous
        assert first.items[0].requester_discord_user_id == "1000"
    finally:
        runtime.dispose()
