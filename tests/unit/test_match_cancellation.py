"""Native whole-Match cancellation Application tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner
from uma_st2.application.match import (
    CancelledMatch,
    CancelMatch,
    MatchCancellationAuditType,
    MatchCancellationBet,
    MatchCancellationCommands,
    MatchCancellationIdempotencyConflictError,
    MatchCancellationInvalidSourceError,
    MatchCancellationRefund,
    MatchCancellationRefundPlan,
    MatchCancellationTarget,
    MatchCancellationUnavailableError,
    MatchCancellationWallet,
    MatchCancellationWalletUnavailableError,
    StoredMatchCancellationOperation,
    StoredMatchRefundPublication,
)
from uma_st2.application.publication import MatchPublicationDestination, PublicationIntent
from uma_st2.domain.match import MatchGrade, MatchSourceKind, MatchStatus

NOW = datetime(2026, 8, 28, 11, 0, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _target(
    *,
    status: MatchStatus = MatchStatus.BETTING_CLOSED,
    source_kind: MatchSourceKind = MatchSourceKind.NATIVE_V2,
    with_bets: bool = True,
) -> MatchCancellationTarget:
    bets = (
        (
            MatchCancellationBet(id=101, persona_id="persona-a", amount=10),
            MatchCancellationBet(id=102, persona_id="persona-a", amount=20),
            MatchCancellationBet(id=103, persona_id="persona-b", amount=30),
        )
        if with_bets
        else ()
    )
    wallets = (
        (
            MatchCancellationWallet(persona_id="persona-a", balance=100),
            MatchCancellationWallet(persona_id="persona-b", balance=200),
        )
        if with_bets
        else ()
    )
    return MatchCancellationTarget(
        match_id=71,
        match_name="제12회 @everyone 정기전",
        source_kind=source_kind,
        status=status,
        terminal_reason=None,
        grade=MatchGrade.G1,
        scheduled_at=SCHEDULED_AT,
        entry_count=3,
        active_bets=bets,
        wallets=wallets,
        destination=MatchPublicationDestination(
            guild_id="987654321",
            announcements_enabled=True,
            target_channel_id="777777777",
        ),
    )


def _command(
    *,
    key: str = "match-cancel:555",
    match_id: int = 71,
    reason: str | None = "공식 경기 취소",
) -> CancelMatch:
    return CancelMatch(
        match_id=match_id,
        reason=reason,
        idempotency_key=key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id="555",
    )


class RecordingRepository:
    def __init__(
        self,
        target: MatchCancellationTarget,
        *,
        stored: StoredMatchCancellationOperation | None = None,
        lock_error: ValueError | None = None,
    ) -> None:
        self.target = target
        self.stored = stored
        self.lock_error = lock_error
        self.calls: list[str] = []
        self.refund_plans: tuple[MatchCancellationRefundPlan, ...] = ()
        self.publication_intent: PublicationIntent | None = None

    def lock_target(self, *, match_id: int, guild_id: str) -> MatchCancellationTarget | None:
        self.calls.append("lock_target")
        if self.lock_error is not None:
            raise self.lock_error
        return self.target

    def find_operation(self, *, idempotency_key: str) -> StoredMatchCancellationOperation | None:
        self.calls.append("find_operation")
        return self.stored

    def cancel_and_refund(
        self,
        *,
        command: CancelMatch,
        target: MatchCancellationTarget,
        refund_plans: tuple[MatchCancellationRefundPlan, ...],
        publication_intent: PublicationIntent | None,
        cancelled_at: datetime,
    ) -> CancelledMatch:
        self.calls.append("cancel_and_refund")
        self.refund_plans = refund_plans
        self.publication_intent = publication_intent
        refunds = tuple(
            MatchCancellationRefund(
                persona_id=plan.persona_id,
                bet_ids=plan.bet_ids,
                amount=plan.amount,
                balance_before=plan.balance_before,
                balance_after=plan.balance_after,
                point_transaction_id=1000 + index,
            )
            for index, plan in enumerate(refund_plans, start=1)
        )
        return CancelledMatch(
            match_id=target.match_id,
            match_name=target.match_name,
            previous_status=target.status,
            status=MatchStatus.CANCELLED,
            reason=command.reason,
            cancelled_at=cancelled_at,
            entry_count=target.entry_count,
            cancelled_bet_count=target.active_bet_count,
            refund_total=target.refund_total,
            refunds=refunds,
            publication=(
                StoredMatchRefundPublication(
                    publication_id=77,
                    event_key=publication_intent.event_key,
                    payload_fingerprint=publication_intent.payload_fingerprint,
                    status=publication_intent.status,
                    target_channel_id=publication_intent.target_channel_id,
                )
                if publication_intent is not None
                else None
            ),
        )


@dataclass
class RecordingUnitOfWork:
    match_cancellation: RecordingRepository
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


def _commands(repository: RecordingRepository) -> tuple[MatchCancellationCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return MatchCancellationCommands(CommandRunner(factory), clock=lambda: NOW), factory


def test_cancellation_aggregates_exact_refunds_per_persona() -> None:
    repository = RecordingRepository(_target())
    commands, factory = _commands(repository)

    result = commands.cancel_match(_command())

    assert result.status == MatchStatus.CANCELLED
    assert result.cancelled_bet_count == 3
    assert result.refund_total == 60
    assert repository.refund_plans == (
        MatchCancellationRefundPlan(
            persona_id="persona-a",
            bet_ids=(101, 102),
            amount=30,
            balance_before=100,
            balance_after=130,
        ),
        MatchCancellationRefundPlan(
            persona_id="persona-b",
            bet_ids=(103,),
            amount=30,
            balance_before=200,
            balance_after=230,
        ),
    )
    assert repository.publication_intent is not None
    assert repository.publication_intent.event_type == "room_match_bets_refunded"
    assert repository.publication_intent.payload_json["refund"] == {
        "completed_at": NOW.isoformat(),
        "full_original_stake": True,
        "reason": "공식 경기 취소",
    }
    assert repository.calls == ["lock_target", "find_operation", "cancel_and_refund"]
    assert factory.created[0].commits == 1


@pytest.mark.parametrize(
    "status",
    (
        MatchStatus.SCHEDULED,
        MatchStatus.BETTING_OPEN,
        MatchStatus.BETTING_CLOSED,
        MatchStatus.RESULT_CONFIRMED,
    ),
)
def test_all_pre_settlement_states_are_cancellable(status: MatchStatus) -> None:
    repository = RecordingRepository(_target(status=status, with_bets=False))
    commands, _ = _commands(repository)

    result = commands.cancel_match(_command())

    assert result.previous_status == status
    assert result.refunds == ()
    assert result.refund_total == 0
    assert result.publication is None


def test_optional_reason_is_omitted_from_refund_payload_without_blocking_cancellation() -> None:
    repository = RecordingRepository(_target())
    commands, _ = _commands(repository)

    result = commands.cancel_match(_command(reason=None))

    assert result.reason is None
    assert result.publication is not None
    assert repository.publication_intent is not None
    assert repository.publication_intent.payload_json["refund"]["reason"] is None


@pytest.mark.parametrize(
    "target",
    (
        _target(status=MatchStatus.SETTLED, with_bets=False),
        _target(status=MatchStatus.CANCELLED, with_bets=False),
        _target(source_kind=MatchSourceKind.IMPORTED_V1, with_bets=False),
    ),
)
def test_terminal_or_imported_target_is_zero_write(target: MatchCancellationTarget) -> None:
    repository = RecordingRepository(target)
    commands, factory = _commands(repository)

    with pytest.raises(MatchCancellationUnavailableError):
        commands.cancel_match(_command())

    assert repository.calls == ["lock_target", "find_operation"]
    assert factory.created[0].rollbacks == 1


def test_missing_wallet_and_malformed_source_are_zero_write() -> None:
    wallet_repository = RecordingRepository(
        _target(),
        lock_error=MatchCancellationWalletUnavailableError("missing wallet"),
    )
    wallet_commands, wallet_factory = _commands(wallet_repository)
    with pytest.raises(MatchCancellationWalletUnavailableError):
        wallet_commands.cancel_match(_command())
    assert wallet_factory.created[0].rollbacks == 1

    malformed_repository = RecordingRepository(_target(), lock_error=ValueError("bad marker"))
    malformed_commands, malformed_factory = _commands(malformed_repository)
    with pytest.raises(MatchCancellationInvalidSourceError):
        malformed_commands.cancel_match(_command())
    assert malformed_factory.created[0].rollbacks == 1


def test_exact_retry_returns_stored_receipt_and_changed_reason_conflicts() -> None:
    command = _command()
    initial_repository = RecordingRepository(_target())
    initial_commands, _ = _commands(initial_repository)
    committed = initial_commands.cancel_match(command)
    stored = StoredMatchCancellationOperation(
        request_fingerprint=command.request_fingerprint,
        type=MatchCancellationAuditType.CANCELLED.value,
        match_id=command.match_id,
        after_data=committed.to_audit_payload(),
    )
    retry_repository = RecordingRepository(
        _target(status=MatchStatus.CANCELLED, with_bets=False),
        stored=stored,
    )
    retry_commands, retry_factory = _commands(retry_repository)

    assert retry_commands.cancel_match(command) == committed
    assert retry_repository.calls == ["lock_target", "find_operation"]
    assert retry_factory.created[0].commits == 1

    conflict_repository = RecordingRepository(
        _target(status=MatchStatus.CANCELLED, with_bets=False),
        stored=stored,
    )
    conflict_commands, _ = _commands(conflict_repository)
    with pytest.raises(MatchCancellationIdempotencyConflictError):
        conflict_commands.cancel_match(_command(reason="다른 사유"))


def test_legacy_v1_exact_retry_evidence_remains_readable() -> None:
    command = _command()
    initial_repository = RecordingRepository(_target())
    initial_commands, _ = _commands(initial_repository)
    committed = initial_commands.cancel_match(command)
    legacy_after = committed.to_audit_payload()
    legacy_after["schema_version"] = 1
    legacy_after.pop("publication")
    stored = StoredMatchCancellationOperation(
        request_fingerprint=command.request_fingerprint,
        type=MatchCancellationAuditType.CANCELLED.value,
        match_id=command.match_id,
        after_data=legacy_after,
    )
    retry_repository = RecordingRepository(
        _target(status=MatchStatus.CANCELLED, with_bets=False),
        stored=stored,
    )
    retry_commands, _ = _commands(retry_repository)

    retried = retry_commands.cancel_match(command)

    assert retried.match_id == committed.match_id
    assert retried.refund_total == committed.refund_total
    assert retried.publication is None
