"""SQLite persistence tests for staff GameAccount owner correction."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.identity import (
    CorrectGameAccountOwner,
    StaffGameAccountOwnerCorrectionCommands,
    StaffGameAccountOwnerCorrectionQueries,
    StaffGameAccountOwnerCorrectionStaleError,
    StaffGameAccountOwnerCorrectionWalletMissingError,
)
from uma_st2.domain.identity import GameRegion
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyStaffGameAccountOwnerCorrectionQueryUnitOfWorkFactory,
    SqlAlchemyStaffGameAccountOwnerCorrectionUnitOfWorkFactory,
)
from uma_st2.infrastructure.database.orm import (
    Base,
    CirclePointORM,
    DiscordPublicationORM,
    GameAccountORM,
    IdentityOperationORM,
    OperationORM,
    PersonaORM,
    PointTransactionORM,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
STORED_NOW = NOW.replace(tzinfo=None)
SOURCE_ID = "11111111-2222-4333-8444-555555555555"
TARGET_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _runtime() -> DatabaseRuntime:
    runtime = DatabaseRuntime.from_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(runtime.engine)
    return runtime


def _seed(runtime: DatabaseRuntime, *, target_wallet: bool = True) -> None:
    with runtime.session_factory.begin() as session:
        session.add_all(
            [
                PersonaORM(
                    id=SOURCE_ID,
                    display_name="현재 소유자",
                    status="normal",
                    created_at=STORED_NOW,
                    updated_at=STORED_NOW,
                ),
                PersonaORM(
                    id=TARGET_ID,
                    display_name="새 소유자",
                    status="warning",
                    created_at=STORED_NOW,
                    updated_at=STORED_NOW,
                ),
                GameAccountORM(
                    id=71,
                    persona_id=SOURCE_ID,
                    game_region="KR",
                    uma_pid="123456789",
                    nickname="이동 계정",
                    affiliation="원래 소속",
                    created_at=STORED_NOW,
                    updated_at=STORED_NOW,
                ),
                GameAccountORM(
                    id=72,
                    persona_id=SOURCE_ID,
                    game_region="JP",
                    uma_pid="223456789",
                    nickname="소스 잔여 계정",
                    affiliation=None,
                    created_at=STORED_NOW,
                    updated_at=STORED_NOW,
                ),
                GameAccountORM(
                    id=73,
                    persona_id=TARGET_ID,
                    game_region="JP",
                    uma_pid="323456789",
                    nickname="타깃 기존 계정",
                    affiliation=None,
                    created_at=STORED_NOW,
                    updated_at=STORED_NOW,
                ),
                CirclePointORM(persona_id=SOURCE_ID, balance=700, updated_at=STORED_NOW),
            ]
        )
        if target_wallet:
            session.add(CirclePointORM(persona_id=TARGET_ID, balance=900, updated_at=STORED_NOW))


def _queries(runtime: DatabaseRuntime) -> StaffGameAccountOwnerCorrectionQueries:
    return StaffGameAccountOwnerCorrectionQueries(
        QueryRunner(SqlAlchemyStaffGameAccountOwnerCorrectionQueryUnitOfWorkFactory(runtime.session_factory))
    )


def _commands(runtime: DatabaseRuntime) -> StaffGameAccountOwnerCorrectionCommands:
    return StaffGameAccountOwnerCorrectionCommands(
        CommandRunner(SqlAlchemyStaffGameAccountOwnerCorrectionUnitOfWorkFactory(runtime.session_factory)),
        clock=lambda: NOW,
    )


def _command(runtime: DatabaseRuntime, *, key: str = "owner-correction-1") -> CorrectGameAccountOwner:
    preview = _queries(runtime).get_preview(
        guild_id="987",
        target_persona_id=TARGET_ID,
        game_region=GameRegion.KR,
        uma_pid="123456789",
        evidence_reason="운영 기록과 본인 확인 완료",
    )
    return CorrectGameAccountOwner(
        guild_id="987",
        target_persona_id=TARGET_ID,
        game_region=preview.state.game_region,
        uma_pid=preview.state.uma_pid,
        evidence_reason=preview.evidence_reason,
        corrected_by_discord_user_id="900",
        expected_source_persona_id=preview.state.source_persona_id,
        expected_state_fingerprint=preview.state.state_fingerprint,
        idempotency_key=key,
        correlation_id=key,
    )


def _count(runtime: DatabaseRuntime, model: type[object]) -> int:
    with runtime.session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_owner_correction_atomically_moves_only_current_owner_and_stores_audit() -> None:
    runtime = _runtime()
    try:
        _seed(runtime)
        command = _command(runtime)
        commands = _commands(runtime)

        corrected = commands.correct(command)
        retried = commands.correct(command)

        assert corrected.source_game_account_count == 1
        assert corrected.target_game_account_count == 2
        assert retried.exact_retry is True
        with runtime.session_factory() as session:
            account = session.get(GameAccountORM, 71)
            source_wallet = session.get(CirclePointORM, SOURCE_ID)
            target_wallet = session.get(CirclePointORM, TARGET_ID)
            audit = session.scalar(
                select(IdentityOperationORM).where(IdentityOperationORM.type == "game_account_owner_reassigned")
            )
            operation = session.get(OperationORM, audit.operation_id if audit is not None else -1)

            assert account is not None
            assert (account.persona_id, account.game_region, account.uma_pid) == (
                TARGET_ID,
                "KR",
                "123456789",
            )
            assert (account.nickname, account.affiliation) == ("이동 계정", "원래 소속")
            assert source_wallet is not None and source_wallet.balance == 700
            assert target_wallet is not None and target_wallet.balance == 900
            assert audit is not None
            assert (audit.persona_id, audit.game_account_id, audit.discord_user_id) == (
                TARGET_ID,
                71,
                None,
            )
            assert audit.before_data["source_persona_id"] == SOURCE_ID
            assert audit.after_data["target_persona_id"] == TARGET_ID
            assert operation is not None
            assert (operation.actor_discord_user_id, operation.reason) == (
                "900",
                "운영 기록과 본인 확인 완료",
            )

        assert _count(runtime, OperationORM) == 1
        assert _count(runtime, IdentityOperationORM) == 1
        assert _count(runtime, PointTransactionORM) == 0
        assert _count(runtime, DiscordPublicationORM) == 0
    finally:
        runtime.dispose()


def test_missing_target_wallet_fails_closed_without_repair_or_grant() -> None:
    runtime = _runtime()
    try:
        _seed(runtime, target_wallet=False)

        with pytest.raises(StaffGameAccountOwnerCorrectionWalletMissingError):
            _queries(runtime).get_preview(
                guild_id="987",
                target_persona_id=TARGET_ID,
                game_region=GameRegion.KR,
                uma_pid="123456789",
                evidence_reason="확인",
            )

        with runtime.session_factory() as session:
            account = session.get(GameAccountORM, 71)
            assert account is not None and account.persona_id == SOURCE_ID
        assert _count(runtime, CirclePointORM) == 1
        assert _count(runtime, PointTransactionORM) == 0
        assert _count(runtime, OperationORM) == 0
    finally:
        runtime.dispose()


def test_preview_fingerprint_change_rejects_without_partial_owner_move() -> None:
    runtime = _runtime()
    try:
        _seed(runtime)
        command = _command(runtime, key="owner-correction-stale")
        with runtime.session_factory.begin() as session:
            session.add(
                GameAccountORM(
                    id=74,
                    persona_id=TARGET_ID,
                    game_region="KR",
                    uma_pid="423456789",
                    nickname="Preview 이후 추가",
                    affiliation=None,
                    created_at=STORED_NOW,
                    updated_at=STORED_NOW,
                )
            )

        with pytest.raises(StaffGameAccountOwnerCorrectionStaleError):
            _commands(runtime).correct(command)

        with runtime.session_factory() as session:
            account = session.get(GameAccountORM, 71)
            assert account is not None and account.persona_id == SOURCE_ID
        assert _count(runtime, OperationORM) == 0
    finally:
        runtime.dispose()


def test_audit_failure_rolls_back_owner_change_and_operation() -> None:
    runtime = _runtime()
    try:
        _seed(runtime)
        command = _command(runtime, key="owner-correction-audit-fail")

        def fail_audit(session: Session, _flush_context: object, _instances: object) -> None:
            if any(isinstance(item, IdentityOperationORM) for item in session.new):
                raise RuntimeError("simulated owner-correction audit failure")

        event.listen(Session, "before_flush", fail_audit)
        try:
            with pytest.raises(RuntimeError, match="simulated owner-correction audit failure"):
                _commands(runtime).correct(command)
        finally:
            event.remove(Session, "before_flush", fail_audit)

        with runtime.session_factory() as session:
            account = session.get(GameAccountORM, 71)
            assert account is not None and account.persona_id == SOURCE_ID
            assert session.get(CirclePointORM, TARGET_ID).balance == 900
        assert _count(runtime, OperationORM) == 0
        assert _count(runtime, IdentityOperationORM) == 0
        assert _count(runtime, PointTransactionORM) == 0
    finally:
        runtime.dispose()
