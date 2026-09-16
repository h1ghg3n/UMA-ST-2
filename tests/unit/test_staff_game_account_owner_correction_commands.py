"""Application contracts for staff GameAccount owner correction."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.identity import (
    CorrectedGameAccountOwner,
    CorrectGameAccountOwner,
    StaffGameAccountOwnerCorrectionAuditType,
    StaffGameAccountOwnerCorrectionCommands,
    StaffGameAccountOwnerCorrectionIdempotencyConflictError,
    StaffGameAccountOwnerCorrectionQueries,
    StaffGameAccountOwnerCorrectionSameOwnerError,
    StaffGameAccountOwnerCorrectionStaleError,
    StaffGameAccountOwnerCorrectionState,
    StaffGameAccountOwnerCorrectionUnavailableError,
    StaffGameAccountOwnerCorrectionWalletMissingError,
    StoredStaffGameAccountOwnerCorrectionOperation,
)
from uma_st2.domain.identity import GameRegion, PersonaStatus

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
SOURCE_ID = "11111111-2222-4333-8444-555555555555"
TARGET_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _state(**changes: object) -> StaffGameAccountOwnerCorrectionState:
    values: dict[str, object] = {
        "guild_id": "987",
        "game_account_id": 71,
        "game_region": GameRegion.KR,
        "uma_pid": "123456789",
        "nickname": "이동 계정",
        "affiliation": "소속",
        "source_persona_id": SOURCE_ID,
        "source_persona_display_name": "현재 소유자",
        "source_persona_status": PersonaStatus.NORMAL,
        "source_has_wallet": True,
        "source_game_account_count": 2,
        "source_qualifying_game_account_count": 2,
        "target_persona_id": TARGET_ID,
        "target_persona_display_name": "새 소유자",
        "target_persona_status": PersonaStatus.WARNING,
        "target_has_wallet": True,
        "target_game_account_count": 1,
        "target_qualifying_game_account_count": 1,
    }
    values.update(changes)
    return StaffGameAccountOwnerCorrectionState(**values)  # type: ignore[arg-type]


def _command(state: StaffGameAccountOwnerCorrectionState, **changes: object) -> CorrectGameAccountOwner:
    values: dict[str, object] = {
        "guild_id": state.guild_id,
        "target_persona_id": state.target_persona_id,
        "game_region": state.game_region,
        "uma_pid": state.uma_pid,
        "evidence_reason": "운영 기록과 본인 확인 완료",
        "corrected_by_discord_user_id": "900",
        "expected_source_persona_id": state.source_persona_id,
        "expected_state_fingerprint": state.state_fingerprint,
        "idempotency_key": "owner-correction-1",
        "correlation_id": "owner-correction-1",
    }
    values.update(changes)
    return CorrectGameAccountOwner(**values)  # type: ignore[arg-type]


class RecordingRepository:
    def __init__(self, state: StaffGameAccountOwnerCorrectionState | None) -> None:
        self.state = state
        self.operations: dict[str, StoredStaffGameAccountOwnerCorrectionOperation] = {}
        self.calls: list[str] = []
        self.write_count = 0

    def lock_state(self, **_kwargs: object) -> StaffGameAccountOwnerCorrectionState | None:
        self.calls.append("state")
        return self.state

    def find_operation(
        self,
        *,
        idempotency_key: str,
    ) -> StoredStaffGameAccountOwnerCorrectionOperation | None:
        self.calls.append("operation")
        return self.operations.get(idempotency_key)

    def reassign_owner(
        self,
        *,
        command: CorrectGameAccountOwner,
        state: StaffGameAccountOwnerCorrectionState,
        corrected_at: datetime,
    ) -> CorrectedGameAccountOwner:
        self.calls.append("write")
        self.write_count += 1
        result = CorrectedGameAccountOwner(
            game_account_id=state.game_account_id,
            game_region=state.game_region,
            uma_pid=state.uma_pid,
            nickname=state.nickname,
            affiliation=state.affiliation,
            source_persona_id=state.source_persona_id,
            source_persona_display_name=state.source_persona_display_name,
            source_persona_status=state.source_persona_status,
            source_has_wallet=state.source_has_wallet,
            source_game_account_count=state.source_resulting_game_account_count,
            source_qualifying_game_account_count=state.source_resulting_qualifying_game_account_count,
            target_persona_id=state.target_persona_id,
            target_persona_display_name=state.target_persona_display_name,
            target_persona_status=state.target_persona_status,
            target_has_wallet=state.target_has_wallet,
            target_game_account_count=state.target_resulting_game_account_count,
            target_qualifying_game_account_count=state.target_resulting_qualifying_game_account_count,
            evidence_reason=command.evidence_reason,
            corrected_at=corrected_at,
        )
        self.operations[command.idempotency_key] = StoredStaffGameAccountOwnerCorrectionOperation(
            request_fingerprint=command.request_fingerprint,
            type=StaffGameAccountOwnerCorrectionAuditType.OWNER_REASSIGNED.value,
            persona_id=result.target_persona_id,
            game_account_id=result.game_account_id,
            after_data=result.to_audit_payload(),
        )
        return result


class CorrectionUnitOfWork:
    def __init__(self, repository: RecordingRepository) -> None:
        self.staff_game_account_owner_correction = repository

    def __enter__(self) -> CorrectionUnitOfWork:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _commands(repository: RecordingRepository) -> StaffGameAccountOwnerCorrectionCommands:
    return StaffGameAccountOwnerCorrectionCommands(
        CommandRunner(lambda: CorrectionUnitOfWork(repository)),
        clock=lambda: NOW,
    )


def test_owner_correction_commits_once_and_exact_retry_returns_stored_receipt() -> None:
    state = _state()
    repository = RecordingRepository(state)
    commands = _commands(repository)
    command = _command(state)

    corrected = commands.correct(command)
    retried = commands.correct(command)

    assert corrected.source_game_account_count == corrected.source_qualifying_game_account_count == 1
    assert corrected.target_game_account_count == corrected.target_qualifying_game_account_count == 2
    assert corrected.source_member_mutation_eligible is True
    assert corrected.target_member_mutation_eligible is True
    assert corrected.exact_retry is False
    assert retried == replace(corrected, exact_retry=True)
    assert repository.write_count == 1
    assert repository.calls == ["state", "operation", "write", "state", "operation"]


@pytest.mark.parametrize("status", list(PersonaStatus))
def test_owner_correction_preserves_every_target_status(status: PersonaStatus) -> None:
    state = _state(target_persona_status=status)

    result = _commands(RecordingRepository(state)).correct(_command(state))

    assert result.target_persona_status is status
    assert result.target_member_mutation_eligible is (status in {PersonaStatus.NORMAL, PersonaStatus.WARNING})


def test_owner_correction_rejects_missing_wallet_same_owner_and_unavailable_without_write() -> None:
    missing_wallet_state = _state(target_has_wallet=False)
    missing_wallet = RecordingRepository(missing_wallet_state)
    with pytest.raises(StaffGameAccountOwnerCorrectionWalletMissingError):
        _commands(missing_wallet).correct(_command(missing_wallet_state))

    same_owner_state = _state(
        target_persona_id=SOURCE_ID,
        target_persona_display_name="현재 소유자",
        target_persona_status=PersonaStatus.NORMAL,
        target_game_account_count=2,
        target_qualifying_game_account_count=2,
    )
    same_owner = RecordingRepository(same_owner_state)
    with pytest.raises(StaffGameAccountOwnerCorrectionSameOwnerError):
        _commands(same_owner).correct(_command(same_owner_state))

    unavailable = RecordingRepository(None)
    with pytest.raises(StaffGameAccountOwnerCorrectionUnavailableError):
        _commands(unavailable).correct(_command(_state()))

    assert missing_wallet.write_count == same_owner.write_count == unavailable.write_count == 0


def test_owner_correction_rejects_stale_state_and_changed_key_payload() -> None:
    preview_state = _state()
    stale_repository = RecordingRepository(_state(target_game_account_count=2, target_qualifying_game_account_count=2))
    with pytest.raises(StaffGameAccountOwnerCorrectionStaleError):
        _commands(stale_repository).correct(_command(preview_state))

    repository = RecordingRepository(preview_state)
    commands = _commands(repository)
    command = _command(preview_state)
    commands.correct(command)
    with pytest.raises(StaffGameAccountOwnerCorrectionIdempotencyConflictError):
        commands.correct(replace(command, evidence_reason="다른 근거"))

    assert stale_repository.write_count == 0
    assert repository.write_count == 1


class QueryRepository:
    def __init__(self, state: StaffGameAccountOwnerCorrectionState | None) -> None:
        self.state = state
        self.calls: list[dict[str, object]] = []

    def get_state(self, **kwargs: object) -> StaffGameAccountOwnerCorrectionState | None:
        self.calls.append(kwargs)
        return self.state


class QueryUnitOfWork:
    def __init__(self, repository: QueryRepository) -> None:
        self.staff_game_account_owner_correction = repository

    def __enter__(self) -> QueryUnitOfWork:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def test_owner_correction_preview_normalizes_input_without_write() -> None:
    repository = QueryRepository(_state())
    queries = StaffGameAccountOwnerCorrectionQueries(QueryRunner(lambda: QueryUnitOfWork(repository)))

    preview = queries.get_preview(
        guild_id=" 987 ",
        target_persona_id=f" {TARGET_ID} ",
        game_region=GameRegion.KR,
        uma_pid=" 123456789 ",
        evidence_reason=" 운영 기록 확인 ",
    )

    assert preview.evidence_reason == "운영 기록 확인"
    assert repository.calls == [
        {
            "guild_id": "987",
            "target_persona_id": TARGET_ID,
            "game_region": GameRegion.KR,
            "uma_pid": "123456789",
        }
    ]
    with pytest.raises(ValueError, match="evidence_reason"):
        queries.get_preview(
            guild_id="987",
            target_persona_id=TARGET_ID,
            game_region=GameRegion.KR,
            uma_pid="123456789",
            evidence_reason="   ",
        )
