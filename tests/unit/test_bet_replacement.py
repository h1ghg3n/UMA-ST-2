"""Native member Match Bet replacement Application tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.betting import (
    BetPlacementEntry,
    BetPlacementPersona,
    BetPlacementTarget,
    BetPlacementWallet,
    BetReplacementApprovalPendingError,
    BetReplacementAuditError,
    BetReplacementAuditType,
    BetReplacementBet,
    BetReplacementCommands,
    BetReplacementDuplicateError,
    BetReplacementIdempotencyConflictError,
    BetReplacementIdentityError,
    BetReplacementInvalidSourceError,
    BetReplacementNoChangeError,
    BetReplacementSelectionUnavailableError,
    BetReplacementStakeLimitError,
    BetReplacementUnavailableError,
    BetReplacementWalletUnavailableError,
    PlacedMatchBet,
    ReplacedMatchBet,
    ReplaceMatchBet,
    StoredBetReplacementOperation,
    fingerprint_bet_selection,
)
from uma_st2.application.execution import CommandRunner
from uma_st2.domain.betting import BetStatus, BetType
from uma_st2.domain.identity import PersonaStatus
from uma_st2.domain.match import MatchSourceKind, MatchStatus

NOW = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)


def _entries() -> tuple[BetPlacementEntry, ...]:
    return (
        BetPlacementEntry(id=101, entry_number=1),
        BetPlacementEntry(id=102, entry_number=2),
        BetPlacementEntry(id=103, entry_number=3),
    )


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
        entries=_entries(),
    )


def _old_bet(
    *,
    persona_id: str = "persona-1",
    status: BetStatus = BetStatus.ACTIVE,
) -> BetReplacementBet:
    selections = _entries()[:2]
    return BetReplacementBet(
        id=801,
        match_id=71,
        persona_id=persona_id,
        bet_type=BetType.QUINELLA,
        selections=selections,
        selection_fingerprint=fingerprint_bet_selection(
            bet_type=BetType.QUINELLA,
            selection_ids=tuple(entry.id for entry in selections),
        ),
        amount=20,
        status=status,
    )


def _command(
    *,
    key: str = "match-bet-change:555",
    bet_type: BetType = BetType.QUINELLA,
    entry_numbers: tuple[int, ...] = (2, 1),
    amount: int = 30,
) -> ReplaceMatchBet:
    return ReplaceMatchBet(
        bet_id=801,
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
        candidate_match_id: int | None = 71,
        target: BetPlacementTarget | None = None,
        stored: StoredBetReplacementOperation | None = None,
        old_bet: BetReplacementBet | None = None,
        persona: BetPlacementPersona | None = None,
        wallet: BetPlacementWallet | None = None,
        wallet_missing: bool = False,
        duplicate_id: int | None = None,
    ) -> None:
        self.candidate_match_id = candidate_match_id
        self.target = target if target is not None else _target()
        self.stored = stored
        self.old_bet = old_bet if old_bet is not None else _old_bet()
        self.persona = persona or BetPlacementPersona(
            id="persona-1",
            status=PersonaStatus.NORMAL,
            has_eligible_game_account=True,
        )
        self.wallet = (
            None
            if wallet_missing
            else wallet
            if wallet is not None
            else BetPlacementWallet(persona_id="persona-1", balance=480)
        )
        self.duplicate_id = duplicate_id
        self.calls: list[str] = []

    def resolve_candidate_match_id(self, *, bet_id: int) -> int | None:
        self.calls.append("resolve_candidate_match_id")
        return self.candidate_match_id

    def lock_target(self, *, match_id: int) -> BetPlacementTarget | None:
        self.calls.append("lock_target")
        return self.target

    def find_operation(self, *, idempotency_key: str) -> StoredBetReplacementOperation | None:
        self.calls.append("find_operation")
        return self.stored

    def lock_bet(self, *, bet_id: int, match_id: int) -> BetReplacementBet | None:
        self.calls.append("lock_bet")
        return self.old_bet

    def lock_actor_persona(self, *, discord_user_id: str) -> BetPlacementPersona | None:
        self.calls.append("lock_actor_persona")
        return self.persona

    def lock_wallet(self, *, persona_id: str) -> BetPlacementWallet | None:
        self.calls.append("lock_wallet")
        return self.wallet

    def find_active_duplicate(self, **kwargs: object) -> int | None:
        self.calls.append("find_active_duplicate")
        return self.duplicate_id

    def persist_replacement(
        self,
        *,
        command: ReplaceMatchBet,
        target: BetPlacementTarget,
        persona: BetPlacementPersona,
        wallet: BetPlacementWallet,
        old_bet: BetReplacementBet,
        selections: tuple[BetPlacementEntry, ...],
        selection_fingerprint: str,
        balance_after_refund: int,
        balance_after: int,
        replaced_at: datetime,
    ) -> ReplacedMatchBet:
        self.calls.append("persist_replacement")
        placed = PlacedMatchBet(
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
            placed_at=replaced_at,
        )
        return ReplacedMatchBet(
            old_bet=old_bet.with_status(BetStatus.CANCELLED),
            new_bet=placed,
            balance_before=wallet.balance,
            balance_after_refund=balance_after_refund,
            balance_after=balance_after,
            refund_point_transaction_id=1001,
            stake_point_transaction_id=1002,
            replaced_at=replaced_at,
        )


@dataclass
class RecordingUnitOfWork:
    bet_replacement: RecordingRepository
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


def _commands(repository: RecordingRepository) -> tuple[BetReplacementCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return BetReplacementCommands(CommandRunner(factory), clock=lambda: NOW), factory


def test_replace_bet_locks_match_first_and_returns_refund_debit_receipt() -> None:
    repository = RecordingRepository()
    commands, factory = _commands(repository)

    result = commands.replace_bet(_command(amount=30))

    assert result.old_bet.id == 801
    assert result.old_bet.status is BetStatus.CANCELLED
    assert result.new_bet.bet_id == 901
    assert result.balance_before == 480
    assert result.balance_after_refund == 500
    assert result.balance_after == 470
    assert repository.calls == [
        "resolve_candidate_match_id",
        "lock_target",
        "find_operation",
        "lock_bet",
        "lock_actor_persona",
        "lock_wallet",
        "find_active_duplicate",
        "persist_replacement",
    ]
    assert factory.created[0].commits == 1


def test_exact_no_change_is_zero_write_but_amount_only_change_is_allowed() -> None:
    no_change = RecordingRepository()
    commands, factory = _commands(no_change)

    with pytest.raises(BetReplacementNoChangeError):
        commands.replace_bet(_command(amount=20))

    assert "find_active_duplicate" not in no_change.calls
    assert "persist_replacement" not in no_change.calls
    assert factory.created[0].rollbacks == 1

    amount_change = RecordingRepository()
    assert _commands(amount_change)[0].replace_bet(_command(amount=30)).new_bet.amount == 30


def test_unknown_selection_duplicate_and_stake_limit_are_zero_write() -> None:
    unknown = RecordingRepository()
    with pytest.raises(BetReplacementSelectionUnavailableError):
        _commands(unknown)[0].replace_bet(_command(entry_numbers=(1, 9)))
    assert "find_active_duplicate" not in unknown.calls

    duplicate = RecordingRepository(duplicate_id=802)
    with pytest.raises(BetReplacementDuplicateError):
        _commands(duplicate)[0].replace_bet(_command(entry_numbers=(1, 3)))
    assert "persist_replacement" not in duplicate.calls

    over_limit = RecordingRepository(wallet=BetPlacementWallet(persona_id="persona-1", balance=0))
    with pytest.raises(BetReplacementStakeLimitError) as captured:
        _commands(over_limit)[0].replace_bet(_command(amount=30))
    assert captured.value.maximum_stake == 10
    assert "persist_replacement" not in over_limit.calls


def test_replacement_stake_limit_uses_exact_post_refund_balance() -> None:
    boundary = RecordingRepository(wallet=BetPlacementWallet(persona_id="persona-1", balance=480))
    result = _commands(boundary)[0].replace_bet(_command(entry_numbers=(1, 3), amount=50))

    assert result.balance_after_refund == 500
    assert result.balance_after == 450

    over_limit = RecordingRepository(wallet=BetPlacementWallet(persona_id="persona-1", balance=480))
    commands, factory = _commands(over_limit)
    with pytest.raises(BetReplacementStakeLimitError) as captured:
        commands.replace_bet(_command(entry_numbers=(1, 3), amount=60))

    assert captured.value.maximum_stake == 50
    assert "persist_replacement" not in over_limit.calls
    assert factory.created[0].rollbacks == 1


def test_stale_foreign_and_ineligible_actor_are_zero_write() -> None:
    closed = RecordingRepository(target=_target(status=MatchStatus.BETTING_CLOSED))
    with pytest.raises(BetReplacementUnavailableError):
        _commands(closed)[0].replace_bet(_command())
    assert "lock_bet" not in closed.calls

    foreign = RecordingRepository(old_bet=_old_bet(persona_id="persona-2"))
    with pytest.raises(BetReplacementUnavailableError):
        _commands(foreign)[0].replace_bet(_command())
    assert "lock_wallet" not in foreign.calls

    ineligible = RecordingRepository(
        persona=BetPlacementPersona(
            id="persona-1",
            status=PersonaStatus.NORMAL,
            has_eligible_game_account=False,
        )
    )
    with pytest.raises(BetReplacementIdentityError):
        _commands(ineligible)[0].replace_bet(_command())
    assert "lock_wallet" not in ineligible.calls

    missing_wallet = RecordingRepository(wallet_missing=True)
    with pytest.raises(BetReplacementWalletUnavailableError):
        _commands(missing_wallet)[0].replace_bet(_command())
    assert "find_active_duplicate" not in missing_wallet.calls


def test_pending_approval_persona_gets_distinct_zero_write_rejection() -> None:
    repository = RecordingRepository(
        persona=BetPlacementPersona(
            id="persona-1",
            status=PersonaStatus.PENDING_APPROVAL,
            has_eligible_game_account=True,
        )
    )
    commands, factory = _commands(repository)

    with pytest.raises(BetReplacementApprovalPendingError, match="approval is pending"):
        commands.replace_bet(_command())

    assert "lock_wallet" not in repository.calls
    assert "persist_replacement" not in repository.calls
    assert factory.created[0].rollbacks == 1


def test_malformed_locked_bet_target_is_rejected_before_identity_lookup() -> None:
    wrong_match = BetReplacementBet(
        id=801,
        match_id=72,
        persona_id="persona-1",
        bet_type=BetType.QUINELLA,
        selections=_entries()[:2],
        selection_fingerprint=fingerprint_bet_selection(
            bet_type=BetType.QUINELLA,
            selection_ids=(101, 102),
        ),
        amount=20,
        status=BetStatus.ACTIVE,
    )
    repository = RecordingRepository(old_bet=wrong_match)

    with pytest.raises(BetReplacementInvalidSourceError):
        _commands(repository)[0].replace_bet(_command())

    assert "lock_actor_persona" not in repository.calls


def test_exact_retry_survives_close_and_changed_payload_conflicts() -> None:
    command = _command()
    committed = _commands(RecordingRepository())[0].replace_bet(command)
    stored = StoredBetReplacementOperation(
        request_fingerprint=command.request_fingerprint,
        type=BetReplacementAuditType.REPLACED.value,
        match_id=committed.match_id,
        bet_id=committed.new_bet.bet_id,
        after_data=committed.to_audit_payload(),
    )
    repository = RecordingRepository(
        target=_target(status=MatchStatus.BETTING_CLOSED),
        stored=stored,
        old_bet=_old_bet(status=BetStatus.CANCELLED),
        wallet=BetPlacementWallet(persona_id="persona-1", balance=0),
    )
    commands, _ = _commands(repository)

    assert commands.replace_bet(command) == committed
    assert repository.calls == ["resolve_candidate_match_id", "lock_target", "find_operation"]

    with pytest.raises(BetReplacementIdempotencyConflictError):
        commands.replace_bet(_command(key=command.idempotency_key, amount=40))

    malformed = dict(committed.to_audit_payload())
    malformed["balance_after"] = 999
    malformed_repository = RecordingRepository(
        target=_target(status=MatchStatus.BETTING_CLOSED),
        stored=StoredBetReplacementOperation(
            request_fingerprint=command.request_fingerprint,
            type=BetReplacementAuditType.REPLACED.value,
            match_id=committed.match_id,
            bet_id=committed.new_bet.bet_id,
            after_data=malformed,
        ),
    )
    with pytest.raises(BetReplacementAuditError):
        _commands(malformed_repository)[0].replace_bet(command)
