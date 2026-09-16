"""SQLite persistence and Application tests for staff Circle Point operations."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.point import (
    MAX_CIRCLE_POINT_BALANCE,
    ApplyStaffCirclePoint,
    StaffCirclePointAmountError,
    StaffCirclePointCommands,
    StaffCirclePointIdempotencyConflictError,
    StaffCirclePointOperation,
    StaffCirclePointQueries,
    StaffCirclePointStaleError,
    StaffCirclePointWalletUnavailableError,
)
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyStaffCirclePointQueryUnitOfWorkFactory,
    SqlAlchemyStaffCirclePointUnitOfWorkFactory,
)
from uma_st2.infrastructure.database.orm import (
    Base,
    CirclePointORM,
    DiscordPublicationORM,
    IdentityOperationORM,
    OperationORM,
    PersonaORM,
    PointTransactionORM,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
STORED_NOW = NOW.replace(tzinfo=None)
PERSONA_ID = "11111111-2222-4333-8444-555555555555"
NO_WALLET_PERSONA_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _runtime() -> DatabaseRuntime:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    return runtime


def _seed(runtime: DatabaseRuntime, *, balance: int = 700) -> None:
    with runtime.session_factory.begin() as session:
        session.add_all(
            [
                PersonaORM(
                    id=PERSONA_ID,
                    display_name="포인트 대상",
                    status="normal",
                    created_at=STORED_NOW,
                    updated_at=STORED_NOW,
                ),
                PersonaORM(
                    id=NO_WALLET_PERSONA_ID,
                    display_name="지갑 없음",
                    status="withdrawn",
                    created_at=STORED_NOW,
                    updated_at=STORED_NOW,
                ),
                CirclePointORM(
                    persona_id=PERSONA_ID,
                    balance=balance,
                    updated_at=STORED_NOW,
                ),
            ]
        )


def _queries(runtime: DatabaseRuntime) -> StaffCirclePointQueries:
    return StaffCirclePointQueries(
        QueryRunner(SqlAlchemyStaffCirclePointQueryUnitOfWorkFactory(runtime.session_factory))
    )


def _commands(runtime: DatabaseRuntime) -> StaffCirclePointCommands:
    return StaffCirclePointCommands(
        CommandRunner(SqlAlchemyStaffCirclePointUnitOfWorkFactory(runtime.session_factory)),
        clock=lambda: NOW,
    )


def _command(
    preview: object,
    *,
    key: str,
    amount: int | None = None,
) -> ApplyStaffCirclePoint:
    operation = preview.operation  # type: ignore[attr-defined]
    return ApplyStaffCirclePoint(
        guild_id="987",
        target_persona_id=preview.state.persona_id,  # type: ignore[attr-defined]
        operation=operation,
        amount=preview.amount if amount is None else amount,  # type: ignore[attr-defined]
        reason=preview.reason,  # type: ignore[attr-defined]
        expected_target_fingerprint=preview.state.state_fingerprint,  # type: ignore[attr-defined]
        actor_discord_user_id="900",
        idempotency_key=key,
        correlation_id=key,
    )


def _count(runtime: DatabaseRuntime, model: type[object]) -> int:
    with runtime.session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_grant_updates_wallet_and_appends_one_operation_transaction_with_exact_retry() -> None:
    runtime = _runtime()
    try:
        _seed(runtime)
        choices = _queries(runtime).search_targets(query="", limit=25)
        preview = _queries(runtime).get_preview(
            guild_id="987",
            persona_id=PERSONA_ID,
            operation=StaffCirclePointOperation.GRANT,
            amount=100,
            reason="이벤트 지급",
        )
        command = _command(preview, key="staff-point-grant-1")

        result = _commands(runtime).apply(command)
        retry = _commands(runtime).apply(command)

        assert [(choice.display_name, choice.wallet_available) for choice in choices] == [
            ("지갑 없음", False),
            ("포인트 대상", True),
        ]
        assert result.action == "manual_grant"
        assert result.amount == 100
        assert result.current_balance == 800
        assert retry.transaction_id == result.transaction_id
        assert retry.exact_retry is True
        assert retry.current_balance == 800
        with runtime.session_factory() as session:
            wallet = session.get(CirclePointORM, PERSONA_ID)
            operation = session.scalar(select(OperationORM))
            transaction = session.scalar(select(PointTransactionORM))
            assert wallet is not None and wallet.balance == 800
            assert operation is not None
            assert operation.guild_id == "987"
            assert operation.actor_discord_user_id == "900"
            assert operation.reason == "이벤트 지급"
            assert transaction is not None
            assert (transaction.persona_id, transaction.operation_id, transaction.action, transaction.amount) == (
                PERSONA_ID,
                operation.id,
                "manual_grant",
                100,
            )
        assert _count(runtime, OperationORM) == 1
        assert _count(runtime, PointTransactionORM) == 1
        assert _count(runtime, IdentityOperationORM) == 0
        assert _count(runtime, DiscordPublicationORM) == 0
    finally:
        runtime.dispose()


def test_signed_adjustment_and_terminal_persona_are_allowed_but_missing_wallet_is_not_repaired() -> None:
    runtime = _runtime()
    try:
        _seed(runtime)
        with runtime.session_factory.begin() as session:
            persona = session.get(PersonaORM, PERSONA_ID)
            assert persona is not None
            persona.status = "expelled"

        preview = _queries(runtime).get_preview(
            guild_id="987",
            persona_id=PERSONA_ID,
            operation=StaffCirclePointOperation.ADJUSTMENT,
            amount=-250,
            reason="중복 반영 정정",
        )
        result = _commands(runtime).apply(_command(preview, key="staff-point-adjust-1"))

        assert result.status.value == "expelled"
        assert result.action == "manual_adjustment"
        assert result.current_balance == 450
        with pytest.raises(StaffCirclePointWalletUnavailableError):
            _queries(runtime).get_preview(
                guild_id="987",
                persona_id=NO_WALLET_PERSONA_ID,
                operation=StaffCirclePointOperation.GRANT,
                amount=10,
                reason="지갑 수리 금지",
            )
        with runtime.session_factory() as session:
            assert session.get(CirclePointORM, NO_WALLET_PERSONA_ID) is None
    finally:
        runtime.dispose()


def test_stale_preview_and_changed_key_payload_are_zero_write() -> None:
    runtime = _runtime()
    try:
        _seed(runtime)
        preview = _queries(runtime).get_preview(
            guild_id="987",
            persona_id=PERSONA_ID,
            operation="grant",
            amount=50,
            reason="첫 지급",
        )
        command = _command(preview, key="staff-point-shared")
        _commands(runtime).apply(command)

        with pytest.raises(StaffCirclePointIdempotencyConflictError):
            _commands(runtime).apply(
                ApplyStaffCirclePoint(
                    guild_id=command.guild_id,
                    target_persona_id=command.target_persona_id,
                    operation=command.operation,
                    amount=60,
                    reason=command.reason,
                    expected_target_fingerprint=command.expected_target_fingerprint,
                    actor_discord_user_id=command.actor_discord_user_id,
                    idempotency_key=command.idempotency_key,
                )
            )

        with pytest.raises(StaffCirclePointStaleError):
            _commands(runtime).apply(_command(preview, key="staff-point-stale"))

        with runtime.session_factory() as session:
            wallet = session.get(CirclePointORM, PERSONA_ID)
            assert wallet is not None and wallet.balance == 750
        assert _count(runtime, OperationORM) == 1
        assert _count(runtime, PointTransactionORM) == 1
    finally:
        runtime.dispose()


@pytest.mark.parametrize(
    ("balance", "operation", "amount"),
    [
        (0, StaffCirclePointOperation.ADJUSTMENT, -1),
        (MAX_CIRCLE_POINT_BALANCE, StaffCirclePointOperation.GRANT, 1),
        (100, StaffCirclePointOperation.ADJUSTMENT, 0),
        (100, StaffCirclePointOperation.GRANT, -1),
    ],
)
def test_invalid_or_out_of_range_amount_is_zero_write(
    balance: int,
    operation: StaffCirclePointOperation,
    amount: int,
) -> None:
    runtime = _runtime()
    try:
        _seed(runtime, balance=balance)
        with pytest.raises(StaffCirclePointAmountError):
            _queries(runtime).get_preview(
                guild_id="987",
                persona_id=PERSONA_ID,
                operation=operation,
                amount=amount,
                reason="범위 검증",
            )
        assert _count(runtime, OperationORM) == 0
        assert _count(runtime, PointTransactionORM) == 0
    finally:
        runtime.dispose()


def test_point_transaction_flush_failure_rolls_back_wallet_and_operation() -> None:
    runtime = _runtime()
    try:
        _seed(runtime)
        preview = _queries(runtime).get_preview(
            guild_id="987",
            persona_id=PERSONA_ID,
            operation="grant",
            amount=100,
            reason="rollback 검증",
        )

        def fail_transaction(session: Session, _flush_context: object, _instances: object) -> None:
            if any(isinstance(item, PointTransactionORM) for item in session.new):
                raise RuntimeError("simulated Point transaction failure")

        event.listen(Session, "before_flush", fail_transaction)
        try:
            with pytest.raises(RuntimeError, match="simulated Point transaction failure"):
                _commands(runtime).apply(_command(preview, key="staff-point-failure"))
        finally:
            event.remove(Session, "before_flush", fail_transaction)

        with runtime.session_factory() as session:
            wallet = session.get(CirclePointORM, PERSONA_ID)
            assert wallet is not None and wallet.balance == 700
        assert _count(runtime, OperationORM) == 0
        assert _count(runtime, PointTransactionORM) == 0
    finally:
        runtime.dispose()
