"""Native member Match Bet placement Application tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.betting import (
    BetPlacementApprovalPendingError,
    BetPlacementAuditError,
    BetPlacementAuditType,
    BetPlacementCommands,
    BetPlacementDuplicateError,
    BetPlacementEntry,
    BetPlacementIdempotencyConflictError,
    BetPlacementIdentityError,
    BetPlacementInsufficientBalanceError,
    BetPlacementPersona,
    BetPlacementSelectionUnavailableError,
    BetPlacementStakeLimitError,
    BetPlacementTarget,
    BetPlacementUnavailableError,
    BetPlacementWallet,
    BetPlacementWalletUnavailableError,
    PlacedMatchBet,
    PlaceMatchBet,
    StoredBetPlacementOperation,
    fingerprint_bet_selection,
)
from uma_st2.application.execution import CommandRunner
from uma_st2.domain.betting import BetStatus, BetType
from uma_st2.domain.identity import PersonaStatus
from uma_st2.domain.match import MatchSourceKind, MatchStatus

NOW = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)


def _target(
    *,
    status: MatchStatus = MatchStatus.BETTING_OPEN,
    source_kind: MatchSourceKind = MatchSourceKind.NATIVE_V2,
) -> BetPlacementTarget:
    return BetPlacementTarget(
        match_id=71,
        match_name="제12회 @everyone 정기전",
        source_kind=source_kind,
        status=status,
        entries=(
            BetPlacementEntry(id=101, entry_number=1),
            BetPlacementEntry(id=102, entry_number=2),
            BetPlacementEntry(id=103, entry_number=3),
        ),
    )


def _command(
    *,
    key: str = "match-bet:555",
    match_id: int = 71,
    bet_type: BetType = BetType.QUINELLA,
    entry_numbers: tuple[int, ...] = (2, 1),
    amount: int = 20,
) -> PlaceMatchBet:
    return PlaceMatchBet(
        match_id=match_id,
        bet_type=bet_type,
        entry_numbers=entry_numbers,
        amount=amount,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        idempotency_key=key,
        correlation_id="555",
    )


class RecordingRepository:
    def __init__(
        self,
        *,
        target: BetPlacementTarget | None = None,
        stored: StoredBetPlacementOperation | None = None,
        persona: BetPlacementPersona | None = None,
        wallet: BetPlacementWallet | None = None,
        wallet_missing: bool = False,
        duplicate_id: int | None = None,
    ) -> None:
        self.target = target if target is not None else _target()
        self.stored = stored
        self.persona = persona or BetPlacementPersona(
            id="persona-1",
            status=PersonaStatus.NORMAL,
            has_eligible_game_account=True,
        )
        self.wallet: BetPlacementWallet | None = (
            None if wallet_missing else wallet or BetPlacementWallet(persona_id="persona-1", balance=500)
        )
        self.duplicate_id = duplicate_id
        self.calls: list[str] = []

    def lock_target(self, *, match_id: int) -> BetPlacementTarget | None:
        self.calls.append("lock_target")
        return self.target

    def find_operation(self, *, idempotency_key: str) -> StoredBetPlacementOperation | None:
        self.calls.append("find_operation")
        return self.stored

    def lock_actor_persona(self, *, discord_user_id: str) -> BetPlacementPersona | None:
        self.calls.append("lock_actor_persona")
        return self.persona

    def lock_wallet(self, *, persona_id: str) -> BetPlacementWallet | None:
        self.calls.append("lock_wallet")
        return self.wallet

    def find_active_duplicate(self, **kwargs: object) -> int | None:
        self.calls.append("find_active_duplicate")
        return self.duplicate_id

    def persist_placement(
        self,
        *,
        command: PlaceMatchBet,
        target: BetPlacementTarget,
        persona: BetPlacementPersona,
        wallet: BetPlacementWallet,
        selections: tuple[BetPlacementEntry, ...],
        selection_fingerprint: str,
        balance_after: int,
        placed_at: datetime,
    ) -> PlacedMatchBet:
        self.calls.append("persist_placement")
        return PlacedMatchBet(
            bet_id=901,
            match_id=target.match_id,
            match_name=target.match_name,
            persona_id=persona.id,
            bet_type=command.bet_type,
            selections=selections,
            selection_fingerprint=selection_fingerprint,
            amount=command.amount,
            status=BetStatus.ACTIVE,
            balance_after=balance_after,
            placed_at=placed_at,
        )


@dataclass
class RecordingUnitOfWork:
    bet_placement: RecordingRepository
    commits: int = 0
    rollbacks: int = 0

    def __enter__(self) -> RecordingUnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if self.commits == 0 and self.rollbacks == 0:
            self.rollbacks += 1
        return False

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class RecordingFactory:
    def __init__(self, repository: RecordingRepository) -> None:
        self.repository = repository
        self.created: list[RecordingUnitOfWork] = []

    def __call__(self) -> RecordingUnitOfWork:
        unit_of_work = RecordingUnitOfWork(self.repository)
        self.created.append(unit_of_work)
        return unit_of_work


def _commands(repository: RecordingRepository) -> tuple[BetPlacementCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return BetPlacementCommands(CommandRunner(factory), clock=lambda: NOW), factory


def test_place_bet_locks_authorities_and_returns_committed_receipt() -> None:
    repository = RecordingRepository()
    commands, factory = _commands(repository)

    result = commands.place_bet(_command())

    assert result.bet_id == 901
    assert tuple(selection.id for selection in result.selections) == (101, 102)
    assert tuple(selection.entry_number for selection in result.selections) == (1, 2)
    assert result.balance_after == 480
    assert repository.calls == [
        "lock_target",
        "find_operation",
        "lock_actor_persona",
        "lock_wallet",
        "find_active_duplicate",
        "persist_placement",
    ]
    assert factory.created[0].commits == 1


def test_exact_retry_survives_later_match_close_and_changed_payload_conflicts() -> None:
    command = _command()
    committed = _commands(RecordingRepository())[0].place_bet(command)
    stored = StoredBetPlacementOperation(
        request_fingerprint=command.request_fingerprint,
        type=BetPlacementAuditType.PLACED.value,
        match_id=command.match_id,
        bet_id=committed.bet_id,
        after_data=committed.to_audit_payload(),
    )
    repository = RecordingRepository(
        target=_target(status=MatchStatus.BETTING_CLOSED),
        stored=stored,
        wallet=BetPlacementWallet(persona_id="persona-1", balance=0),
    )
    commands, _ = _commands(repository)

    assert commands.place_bet(command) == committed
    assert repository.calls == ["lock_target", "find_operation"]

    with pytest.raises(BetPlacementIdempotencyConflictError):
        commands.place_bet(_command(key=command.idempotency_key, amount=30))

    malformed_after = dict(committed.to_audit_payload())
    malformed_after["amount"] = 30
    malformed_repository = RecordingRepository(
        target=_target(status=MatchStatus.BETTING_CLOSED),
        stored=StoredBetPlacementOperation(
            request_fingerprint=command.request_fingerprint,
            type=BetPlacementAuditType.PLACED.value,
            match_id=command.match_id,
            bet_id=committed.bet_id,
            after_data=malformed_after,
        ),
    )
    with pytest.raises(BetPlacementAuditError):
        _commands(malformed_repository)[0].place_bet(command)


@pytest.mark.parametrize(
    "target",
    (
        _target(status=MatchStatus.BETTING_CLOSED),
        _target(source_kind=MatchSourceKind.IMPORTED_V1),
    ),
)
def test_non_open_or_imported_match_is_zero_write(target: BetPlacementTarget) -> None:
    repository = RecordingRepository(target=target)
    commands, factory = _commands(repository)

    with pytest.raises(BetPlacementUnavailableError):
        commands.place_bet(_command())

    assert repository.calls == ["lock_target", "find_operation"]
    assert factory.created[0].rollbacks == 1


@pytest.mark.parametrize(
    "persona",
    (
        BetPlacementPersona(
            id="persona-1",
            status=PersonaStatus.WITHDRAWN,
            has_eligible_game_account=True,
        ),
        BetPlacementPersona(
            id="persona-1",
            status=PersonaStatus.NORMAL,
            has_eligible_game_account=False,
        ),
    ),
)
def test_ineligible_persona_is_zero_write(persona: BetPlacementPersona) -> None:
    repository = RecordingRepository(persona=persona)
    commands, factory = _commands(repository)

    with pytest.raises(BetPlacementIdentityError):
        commands.place_bet(_command())

    assert "lock_wallet" not in repository.calls
    assert factory.created[0].rollbacks == 1


def test_pending_approval_persona_gets_distinct_zero_write_rejection() -> None:
    repository = RecordingRepository(
        persona=BetPlacementPersona(
            id="persona-1",
            status=PersonaStatus.PENDING_APPROVAL,
            has_eligible_game_account=True,
        )
    )
    commands, factory = _commands(repository)

    with pytest.raises(BetPlacementApprovalPendingError, match="approval is pending"):
        commands.place_bet(_command())

    assert "lock_wallet" not in repository.calls
    assert "persist_placement" not in repository.calls
    assert factory.created[0].rollbacks == 1


def test_unknown_selection_and_active_duplicate_are_zero_write() -> None:
    unknown = RecordingRepository()
    commands, _ = _commands(unknown)
    with pytest.raises(BetPlacementSelectionUnavailableError):
        commands.place_bet(_command(entry_numbers=(1, 9)))
    assert "find_active_duplicate" not in unknown.calls

    duplicate = RecordingRepository(duplicate_id=801)
    commands, _ = _commands(duplicate)
    with pytest.raises(BetPlacementDuplicateError):
        commands.place_bet(_command(amount=30))
    assert "persist_placement" not in duplicate.calls


def test_insufficient_balance_is_zero_write() -> None:
    repository = RecordingRepository(wallet=BetPlacementWallet(persona_id="persona-1", balance=0))
    commands, factory = _commands(repository)

    with pytest.raises(BetPlacementInsufficientBalanceError):
        commands.place_bet(_command(amount=10))

    assert "persist_placement" not in repository.calls
    assert factory.created[0].rollbacks == 1


def test_stake_limit_uses_fresh_locked_balance_and_is_zero_write() -> None:
    repository = RecordingRepository(wallet=BetPlacementWallet(persona_id="persona-1", balance=500))
    commands, factory = _commands(repository)

    with pytest.raises(BetPlacementStakeLimitError) as captured:
        commands.place_bet(_command(amount=60))

    assert captured.value.maximum_stake == 50
    assert "persist_placement" not in repository.calls
    assert factory.created[0].rollbacks == 1

    boundary = RecordingRepository(wallet=BetPlacementWallet(persona_id="persona-1", balance=500))
    assert _commands(boundary)[0].place_bet(_command(amount=50)).balance_after == 450


def test_missing_wallet_is_not_created_or_initialized() -> None:
    repository = RecordingRepository(wallet_missing=True)
    commands, factory = _commands(repository)

    with pytest.raises(BetPlacementWalletUnavailableError):
        commands.place_bet(_command())

    assert "find_active_duplicate" not in repository.calls
    assert "persist_placement" not in repository.calls
    assert factory.created[0].rollbacks == 1


def test_selection_fingerprint_uses_entry_ids_not_operator_numbers() -> None:
    assert fingerprint_bet_selection(bet_type=BetType.QUINELLA, selection_ids=(102, 101)) == (
        fingerprint_bet_selection(bet_type=BetType.QUINELLA, selection_ids=(101, 102))
    )
