"""SQLite persistence tests for staff display-information corrections."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.identity import (
    StaffDisplayEditCommands,
    StaffDisplayEditGameAccountNotFoundError,
    StaffDisplayEditQueries,
    UpdateGameAccountDisplayInfo,
    UpdatePersonaDisplayName,
)
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyStaffDisplayEditQueryUnitOfWorkFactory,
    SqlAlchemyStaffDisplayEditUnitOfWorkFactory,
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

NOW = datetime(2026, 9, 3, 11, 0, tzinfo=UTC)
STORED_NOW = NOW.replace(tzinfo=None)
PERSONA_ID = "11111111-2222-4333-8444-555555555555"
OTHER_PERSONA_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


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
                    display_name="기존 Persona",
                    status="withdrawn",
                    created_at=STORED_NOW,
                    updated_at=STORED_NOW,
                ),
                PersonaORM(
                    id=OTHER_PERSONA_ID,
                    display_name="다른 Persona",
                    status="normal",
                    created_at=STORED_NOW,
                    updated_at=STORED_NOW,
                ),
                GameAccountORM(
                    id=1,
                    persona_id=PERSONA_ID,
                    game_region="KR",
                    uma_pid=None,
                    nickname="기존 계정",
                    affiliation="기존 소속",
                    created_at=STORED_NOW,
                    updated_at=STORED_NOW,
                ),
                GameAccountORM(
                    id=2,
                    persona_id=OTHER_PERSONA_ID,
                    game_region="JP",
                    uma_pid="222222222",
                    nickname="다른 계정",
                    affiliation=None,
                    created_at=STORED_NOW,
                    updated_at=STORED_NOW,
                ),
                CirclePointORM(persona_id=PERSONA_ID, balance=700, updated_at=STORED_NOW),
                DiscordAccountORM(
                    id=1,
                    discord_user_id="123456",
                    persona_id=PERSONA_ID,
                    created_at=STORED_NOW,
                    updated_at=STORED_NOW,
                ),
            ]
        )


def _queries(runtime: DatabaseRuntime) -> StaffDisplayEditQueries:
    return StaffDisplayEditQueries(
        QueryRunner(SqlAlchemyStaffDisplayEditQueryUnitOfWorkFactory(runtime.session_factory))
    )


def _commands(runtime: DatabaseRuntime) -> StaffDisplayEditCommands:
    return StaffDisplayEditCommands(
        CommandRunner(SqlAlchemyStaffDisplayEditUnitOfWorkFactory(runtime.session_factory)),
        clock=lambda: NOW,
    )


def _count(runtime: DatabaseRuntime, model: type[object]) -> int:
    with runtime.session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_display_edits_atomically_update_only_selected_current_fields_and_audit() -> None:
    runtime = _runtime()
    try:
        _seed(runtime)
        queries = _queries(runtime)
        persona_preview = queries.get_persona_preview(
            guild_id="987",
            persona_id=PERSONA_ID,
            display_name="수정 Persona",
            reason="표시명 정정",
        )
        account_preview = queries.get_game_account_preview(
            guild_id="987",
            persona_id=PERSONA_ID,
            game_account_id=1,
            nickname="수정 계정",
            affiliation=None,
            reason="소속 제거",
        )
        commands = _commands(runtime)
        persona_command = UpdatePersonaDisplayName(
            guild_id="987",
            target_persona_id=PERSONA_ID,
            display_name=persona_preview.display_name,
            reason=persona_preview.reason,
            updated_by_discord_user_id="900",
            expected_target_fingerprint=persona_preview.state.state_fingerprint,
            idempotency_key="persona-display-1",
        )

        persona_result = commands.update_persona(persona_command)
        persona_retry = commands.update_persona(persona_command)

        # Persona display changes invalidate the account Preview by design.
        account_preview = queries.get_game_account_preview(
            guild_id="987",
            persona_id=PERSONA_ID,
            game_account_id=1,
            nickname=account_preview.nickname,
            affiliation=account_preview.affiliation,
            reason=account_preview.reason,
        )
        account_command = UpdateGameAccountDisplayInfo(
            guild_id="987",
            target_persona_id=PERSONA_ID,
            game_account_id=1,
            nickname=account_preview.nickname,
            affiliation=account_preview.affiliation,
            reason=account_preview.reason,
            updated_by_discord_user_id="900",
            expected_target_fingerprint=account_preview.state.state_fingerprint,
            idempotency_key="account-display-1",
        )
        account_result = commands.update_game_account(account_command)
        account_retry = commands.update_game_account(account_command)

        assert persona_result.status.value == "withdrawn"
        assert persona_retry.exact_retry is True
        assert account_result.uma_pid is None
        assert account_result.previous_affiliation == "기존 소속"
        assert account_retry.exact_retry is True
        with runtime.session_factory() as session:
            persona = session.get(PersonaORM, PERSONA_ID)
            account = session.get(GameAccountORM, 1)
            other = session.get(GameAccountORM, 2)
            wallet = session.get(CirclePointORM, PERSONA_ID)
            link = session.get(DiscordAccountORM, 1)
            audits = session.scalars(select(IdentityOperationORM).order_by(IdentityOperationORM.operation_id)).all()

            assert persona is not None and (persona.display_name, persona.status) == (
                "수정 Persona",
                "withdrawn",
            )
            assert account is not None
            assert (account.game_region, account.uma_pid, account.nickname, account.affiliation) == (
                "KR",
                None,
                "수정 계정",
                None,
            )
            assert other is not None and other.nickname == "다른 계정"
            assert wallet is not None and wallet.balance == 700
            assert link is not None and link.persona_id == PERSONA_ID
            assert [audit.type for audit in audits] == [
                "persona_display_name_updated",
                "game_account_display_info_updated",
            ]
            assert audits[0].game_account_id is None
            assert audits[1].game_account_id == 1
            assert audits[1].before_data["uma_pid"] is None
            assert audits[1].after_data["affiliation"] is None

        assert _count(runtime, OperationORM) == 2
        assert _count(runtime, IdentityOperationORM) == 2
        assert _count(runtime, PointTransactionORM) == 0
        assert _count(runtime, GameAccountRegistrationRequestORM) == 0
        assert _count(runtime, DiscordPublicationORM) == 0
    finally:
        runtime.dispose()


def test_game_account_page_is_bounded_and_wrong_owner_is_zero_write() -> None:
    runtime = _runtime()
    try:
        _seed(runtime)
        with runtime.session_factory.begin() as session:
            session.add_all(
                [
                    GameAccountORM(
                        id=index,
                        persona_id=PERSONA_ID,
                        game_region="KR",
                        uma_pid=str(100000000 + index),
                        nickname=f"계정 {index}",
                        affiliation=None,
                        created_at=STORED_NOW,
                        updated_at=STORED_NOW,
                    )
                    for index in range(3, 24)
                ]
            )

        first = _queries(runtime).list_game_accounts(guild_id="987", persona_id=PERSONA_ID, page=0)
        second = _queries(runtime).list_game_accounts(guild_id="987", persona_id=PERSONA_ID, page=1)

        assert len(first.items) == 20
        assert first.total_count == 22
        assert first.has_next is True
        assert len(second.items) == 2
        assert second.has_previous is True

        with pytest.raises(StaffDisplayEditGameAccountNotFoundError):
            _queries(runtime).get_game_account_preview(
                guild_id="987",
                persona_id=PERSONA_ID,
                game_account_id=2,
                nickname="탈취",
                affiliation=None,
                reason="잘못된 owner",
            )
        assert _count(runtime, OperationORM) == 0
    finally:
        runtime.dispose()


def test_display_edit_audit_failure_rolls_back_current_field_and_operation() -> None:
    runtime = _runtime()
    try:
        _seed(runtime)
        preview = _queries(runtime).get_persona_preview(
            guild_id="987",
            persona_id=PERSONA_ID,
            display_name="수정 Persona",
            reason="표시명 정정",
        )
        command = UpdatePersonaDisplayName(
            guild_id="987",
            target_persona_id=PERSONA_ID,
            display_name=preview.display_name,
            reason=preview.reason,
            updated_by_discord_user_id="900",
            expected_target_fingerprint=preview.state.state_fingerprint,
            idempotency_key="persona-display-fail",
        )

        def fail_audit(session: Session, _flush_context: object, _instances: object) -> None:
            if any(isinstance(item, IdentityOperationORM) for item in session.new):
                raise RuntimeError("simulated display-edit audit failure")

        event.listen(Session, "before_flush", fail_audit)
        try:
            with pytest.raises(RuntimeError, match="simulated display-edit audit failure"):
                _commands(runtime).update_persona(command)
        finally:
            event.remove(Session, "before_flush", fail_audit)

        with runtime.session_factory() as session:
            persona = session.get(PersonaORM, PERSONA_ID)
            assert persona is not None and persona.display_name == "기존 Persona"
        assert _count(runtime, OperationORM) == 0
        assert _count(runtime, IdentityOperationORM) == 0
    finally:
        runtime.dispose()
