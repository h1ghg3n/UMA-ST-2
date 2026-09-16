"""Terminal native V2 Match settlement rollback Application tests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner
from uma_st2.application.match import (
    MATCH_BET_PAYOUT_POINT_ACTION,
    MATCH_BET_PAYOUT_REVERSAL_POINT_ACTION,
    MATCH_BET_REFUND_POINT_ACTION,
    MATCH_PLACEMENT_REWARD_POINT_ACTION,
    MATCH_PLACEMENT_REWARD_REVERSAL_POINT_ACTION,
    MatchSettlementAppliedOdds,
    MatchSettlementOriginalPointTransaction,
    MatchSettlementOriginalRatingTransaction,
    MatchSettlementPayout,
    MatchSettlementPointCompensation,
    MatchSettlementRating,
    MatchSettlementRatingCompensation,
    MatchSettlementResultAuthority,
    MatchSettlementReward,
    MatchSettlementRollbackAuditType,
    MatchSettlementRollbackBalanceError,
    MatchSettlementRollbackBet,
    MatchSettlementRollbackCommands,
    MatchSettlementRollbackEvidenceExpiredError,
    MatchSettlementRollbackInvalidSourceError,
    MatchSettlementRollbackLock,
    MatchSettlementRollbackPlan,
    MatchSettlementRollbackStaleError,
    MatchSettlementRollbackTarget,
    MatchSettlementRollbackUnavailableError,
    MatchSettlementRollbackWallet,
    RollbackMatchSettlement,
    RolledBackMatchSettlement,
    SettledMatch,
    StoredMatchSettlementRollbackOperation,
    build_match_settlement_rollback_plan,
)
from uma_st2.application.match.settlement import MatchSettlementRuleReference
from uma_st2.application.publication import (
    MATCH_SETTLEMENT_VOIDED_EVENT_TYPE,
    MatchPublicationDestination,
    MatchSettlementVoidedPublicationSource,
    PublicationIntent,
)
from uma_st2.domain.betting import BetType
from uma_st2.domain.match import MatchGrade, MatchRatingDisposition, MatchSourceKind, MatchStatus

NOW = datetime(2026, 8, 28, 15, 0, tzinfo=UTC)
SETTLED_AT = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)
RATING_100 = Decimal("100.000000000000000000")
RATING_110 = Decimal("110.000000000000000000")
RATING_90 = Decimal("90.000000000000000000")
RATING_10 = Decimal("10.000000000000000000")
RATING_MINUS_10 = Decimal("-10.000000000000000000")
RATING_ZERO = Decimal("0.000000000000000000")


def _settled() -> SettledMatch:
    return SettledMatch(
        match_id=71,
        match_name="제12회 정기전",
        previous_status=MatchStatus.RESULT_CONFIRMED,
        status=MatchStatus.SETTLED,
        grade=MatchGrade.G1,
        settled_at=SETTLED_AT,
        result=MatchSettlementResultAuthority(301, 2, "a" * 64),
        settlement_fingerprint="b" * 64,
        active_bet_ids=(201, 202),
        active_stake_total=30,
        applied_odds=(
            MatchSettlementAppliedOdds(
                BetType.WIN,
                (11,),
                (1,),
                Decimal("2.0000"),
                Decimal("1.0"),
            ),
            MatchSettlementAppliedOdds(
                BetType.QUINELLA,
                (11, 12),
                (1, 2),
                Decimal("3.0000"),
                Decimal("1.5"),
            ),
        ),
        payouts=(MatchSettlementPayout("persona-a", (201,), 20, 1001),),
        rewards=(
            MatchSettlementReward("persona-a", 11, 101, 1, (), 100, 1002),
            MatchSettlementReward("persona-b", 12, 102, 2, (), 60, 1003),
        ),
        rating_rule_version=MatchSettlementRuleReference(41, 3, "c" * 64),
        ratings=(
            MatchSettlementRating(
                11,
                1,
                101,
                "trainer-a",
                "horse-a",
                "circle-a",
                1,
                RATING_100,
                RATING_10,
                RATING_ZERO,
                RATING_10,
                RATING_110,
                3001,
            ),
            MatchSettlementRating(
                12,
                2,
                102,
                "trainer-b",
                "horse-b",
                "circle-a",
                2,
                RATING_100,
                RATING_MINUS_10,
                RATING_ZERO,
                RATING_MINUS_10,
                RATING_90,
                3002,
            ),
        ),
    )


def _target(
    *,
    source_kind: MatchSourceKind = MatchSourceKind.NATIVE_V2,
    status: MatchStatus = MatchStatus.SETTLED,
) -> MatchSettlementRollbackTarget:
    settlement = _settled()
    return MatchSettlementRollbackTarget(
        match_id=settlement.match_id,
        match_name=settlement.match_name,
        source_kind=source_kind,
        status=status,
        settlement_operation_id=900,
        settlement=settlement,
        bets=(
            MatchSettlementRollbackBet(201, "persona-a", 10),
            MatchSettlementRollbackBet(202, "persona-b", 20),
        ),
        wallets=(
            MatchSettlementRollbackWallet("persona-a", 250),
            MatchSettlementRollbackWallet("persona-b", 160),
        ),
        point_transactions=(
            MatchSettlementOriginalPointTransaction(
                1001,
                900,
                "persona-a",
                MATCH_BET_PAYOUT_POINT_ACTION,
                20,
            ),
            MatchSettlementOriginalPointTransaction(
                1002,
                900,
                "persona-a",
                MATCH_PLACEMENT_REWARD_POINT_ACTION,
                100,
            ),
            MatchSettlementOriginalPointTransaction(
                1003,
                900,
                "persona-b",
                MATCH_PLACEMENT_REWARD_POINT_ACTION,
                60,
            ),
        ),
        rating_transactions=(
            MatchSettlementOriginalRatingTransaction(
                3001,
                900,
                41,
                11,
                101,
                RATING_100,
                RATING_10,
                RATING_110,
                RATING_110,
                3001,
            ),
            MatchSettlementOriginalRatingTransaction(
                3002,
                900,
                41,
                12,
                102,
                RATING_100,
                RATING_MINUS_10,
                RATING_90,
                RATING_90,
                3002,
            ),
        ),
    )


def _command(
    plan: MatchSettlementRollbackPlan, *, key: str = "match-settlement-rollback:555"
) -> RollbackMatchSettlement:
    return RollbackMatchSettlement(
        match_id=plan.target.match_id,
        expected_rollback_fingerprint=plan.rollback_fingerprint,
        idempotency_key=key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        reason="공식 결과 오류",
        correlation_id="555",
    )


def _committed(plan: MatchSettlementRollbackPlan, command: RollbackMatchSettlement) -> RolledBackMatchSettlement:
    return RolledBackMatchSettlement(
        match_id=plan.target.match_id,
        match_name=plan.target.match_name,
        previous_status=MatchStatus.SETTLED,
        status=MatchStatus.VOIDED,
        reason=command.reason,
        rolled_back_at=NOW,
        settlement_operation_id=plan.target.settlement_operation_id,
        settlement_fingerprint=plan.target.settlement.settlement_fingerprint,
        cancelled_bet_ids=(201, 202),
        wallets=plan.wallets,
        point_compensations=(
            MatchSettlementPointCompensation(
                "persona-a",
                MATCH_BET_PAYOUT_REVERSAL_POINT_ACTION,
                -20,
                4001,
                original_point_transaction_id=1001,
            ),
            MatchSettlementPointCompensation(
                "persona-a",
                MATCH_BET_REFUND_POINT_ACTION,
                10,
                4002,
                refunded_bet_ids=(201,),
            ),
            MatchSettlementPointCompensation(
                "persona-a",
                MATCH_PLACEMENT_REWARD_REVERSAL_POINT_ACTION,
                -100,
                4003,
                original_point_transaction_id=1002,
            ),
            MatchSettlementPointCompensation(
                "persona-b",
                MATCH_BET_REFUND_POINT_ACTION,
                20,
                4004,
                refunded_bet_ids=(202,),
            ),
            MatchSettlementPointCompensation(
                "persona-b",
                MATCH_PLACEMENT_REWARD_REVERSAL_POINT_ACTION,
                -60,
                4005,
                original_point_transaction_id=1003,
            ),
        ),
        rating_compensations=(
            MatchSettlementRatingCompensation(3001, 5001, 41, 11, 101, RATING_110, -RATING_10, RATING_100),
            MatchSettlementRatingCompensation(3002, 5002, 41, 12, 102, RATING_90, RATING_10, RATING_100),
        ),
    )


def _publication_source(target: MatchSettlementRollbackTarget) -> MatchSettlementVoidedPublicationSource:
    return MatchSettlementVoidedPublicationSource(
        destination=MatchPublicationDestination("987654321", True, "777777777"),
        match_id=target.match_id,
        match_name=target.match_name,
        grade=target.settlement.grade,
        scheduled_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        rolled_back_at=NOW,
        reason="공식 결과 오류",
    )


class RecordingRepository:
    def __init__(
        self,
        target: MatchSettlementRollbackTarget,
        *,
        stored: StoredMatchSettlementRollbackOperation | None = None,
        publication_source_available: bool = True,
    ) -> None:
        self.target = target
        self.stored = stored
        self.publication_source_available = publication_source_available
        self.calls: list[str] = []
        self.publication_intents: list[PublicationIntent] = []

    def lock_match(self, *, match_id: int) -> MatchSettlementRollbackLock | None:
        self.calls.append("lock_match")
        return MatchSettlementRollbackLock(
            self.target.match_id,
            self.target.match_name,
            self.target.source_kind,
            self.target.status,
        )

    def find_operation(self, *, idempotency_key: str) -> StoredMatchSettlementRollbackOperation | None:
        self.calls.append("find_operation")
        return self.stored

    def load_target(self, *, match_id: int, lock: bool) -> MatchSettlementRollbackTarget | None:
        self.calls.append(f"load_target:{lock}")
        return self.target

    def persist_rollback(
        self,
        *,
        command: RollbackMatchSettlement,
        plan: MatchSettlementRollbackPlan,
        rolled_back_at: datetime,
    ) -> RolledBackMatchSettlement:
        self.calls.append("persist_rollback")
        assert rolled_back_at == NOW
        return _committed(plan, command)

    def load_rollback_publication_source(
        self,
        *,
        match_id: int,
        guild_id: str,
    ) -> MatchSettlementVoidedPublicationSource | None:
        self.calls.append("load_rollback_publication_source")
        assert match_id == self.target.match_id
        assert guild_id == "987654321"
        return _publication_source(self.target) if self.publication_source_available else None

    def add_rollback_publication(
        self,
        *,
        intent: PublicationIntent,
        created_at: datetime,
    ) -> None:
        self.calls.append("add_rollback_publication")
        assert created_at == NOW
        self.publication_intents.append(intent)


@dataclass
class RecordingUnitOfWork:
    match_settlement_rollback: RecordingRepository
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


def _commands(repository: RecordingRepository) -> tuple[MatchSettlementRollbackCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return MatchSettlementRollbackCommands(CommandRunner(factory), clock=lambda: NOW), factory


def test_rollback_plan_builds_exact_point_and_rating_compensation() -> None:
    plan = build_match_settlement_rollback_plan(_target())

    assert plan.stake_refund_total == 30
    assert plan.payout_reversal_total == 20
    assert plan.reward_reversal_total == 160
    assert tuple(
        (
            item.persona_id,
            item.balance_before,
            item.net_delta,
            item.balance_after,
        )
        for item in plan.wallets
    ) == (
        ("persona-a", 250, -110, 140),
        ("persona-b", 160, -40, 120),
    )
    assert tuple(
        (
            item.original_transaction_id,
            item.compensation_before,
            item.compensation_amount,
            item.compensation_after,
        )
        for item in plan.ratings
    ) == (
        (3001, RATING_110, -RATING_10, RATING_100),
        (3002, RATING_90, RATING_10, RATING_100),
    )


def test_later_rating_transaction_expires_complete_rollback() -> None:
    target = _target()
    target = replace(
        target,
        rating_transactions=(
            replace(target.rating_transactions[0], latest_transaction_id=9999),
            target.rating_transactions[1],
        ),
    )

    with pytest.raises(MatchSettlementRollbackEvidenceExpiredError, match="no longer latest"):
        build_match_settlement_rollback_plan(target)


def test_missing_point_evidence_and_insufficient_wallet_are_zero_write_plans() -> None:
    target = _target()
    with pytest.raises(MatchSettlementRollbackEvidenceExpiredError, match="Point coverage"):
        build_match_settlement_rollback_plan(replace(target, point_transactions=target.point_transactions[:-1]))

    with pytest.raises(MatchSettlementRollbackBalanceError):
        build_match_settlement_rollback_plan(
            replace(
                target,
                wallets=(
                    replace(target.wallets[0], balance=0),
                    target.wallets[1],
                ),
            )
        )


def test_op_explicit_empty_rating_bundle_rolls_back_without_rating_compensation() -> None:
    target = _target()
    settlement = replace(
        target.settlement,
        grade=MatchGrade.OP,
        rewards=(),
        rating_rule_version=None,
        ratings=tuple(
            replace(
                item,
                base_delta=RATING_ZERO,
                adjustment_delta=RATING_ZERO,
                amount=RATING_ZERO,
                rating_after=item.rating_before,
                rating_transaction_id=None,
                rating_disposition=MatchRatingDisposition.NOT_APPLICABLE,
                rating_rank=None,
            )
            for item in target.settlement.ratings
        ),
    )
    target = replace(
        target,
        settlement=settlement,
        point_transactions=(target.point_transactions[0],),
        rating_transactions=(),
    )

    plan = build_match_settlement_rollback_plan(target)

    assert plan.ratings == ()
    assert plan.reward_reversal_total == 0
    assert plan.stake_refund_total == 30
    assert plan.payout_reversal_total == 20


def test_command_commits_one_complete_terminal_compensation() -> None:
    target = _target()
    plan = build_match_settlement_rollback_plan(target)
    repository = RecordingRepository(target)
    commands, factory = _commands(repository)

    result = commands.rollback_settlement(_command(plan))

    assert result.status is MatchStatus.VOIDED
    assert result.point_transaction_ids == (4001, 4002, 4003, 4004, 4005)
    assert result.rating_transaction_ids == (5001, 5002)
    assert repository.calls == [
        "lock_match",
        "find_operation",
        "load_target:True",
        "persist_rollback",
        "load_rollback_publication_source",
        "add_rollback_publication",
    ]
    assert len(repository.publication_intents) == 1
    intent = repository.publication_intents[0]
    assert intent.event_type == MATCH_SETTLEMENT_VOIDED_EVENT_TYPE
    assert intent.event_key == f"match:{target.match_id}:settlement-voided:v1"
    assert intent.source_id == target.match_id
    assert intent.payload_json == _publication_source(target).to_payload()
    assert factory.created[0].commits == 1


def test_missing_publication_source_rolls_back_complete_compensation_uow() -> None:
    target = _target()
    plan = build_match_settlement_rollback_plan(target)
    repository = RecordingRepository(target, publication_source_available=False)
    commands, factory = _commands(repository)

    with pytest.raises(MatchSettlementRollbackInvalidSourceError, match="publication evidence"):
        commands.rollback_settlement(_command(plan))

    assert repository.calls[-2:] == ["persist_rollback", "load_rollback_publication_source"]
    assert repository.publication_intents == []
    assert factory.created[0].commits == 0
    assert factory.created[0].rollbacks == 1


def test_stale_preview_is_zero_write() -> None:
    target = _target()
    plan = build_match_settlement_rollback_plan(target)
    repository = RecordingRepository(target)
    commands, factory = _commands(repository)
    command = replace(_command(plan), expected_rollback_fingerprint="f" * 64)

    with pytest.raises(MatchSettlementRollbackStaleError):
        commands.rollback_settlement(command)

    assert repository.calls == ["lock_match", "find_operation", "load_target:True"]
    assert factory.created[0].rollbacks == 1


def test_imported_or_non_settled_target_is_zero_write() -> None:
    for target in (
        _target(source_kind=MatchSourceKind.IMPORTED_V1),
        _target(status=MatchStatus.VOIDED),
    ):
        plan = build_match_settlement_rollback_plan(_target())
        repository = RecordingRepository(target)
        commands, factory = _commands(repository)

        with pytest.raises(MatchSettlementRollbackUnavailableError):
            commands.rollback_settlement(_command(plan))

        assert repository.calls == ["lock_match", "find_operation"]
        assert factory.created[0].rollbacks == 1


def test_same_key_exact_retry_uses_stored_receipt_after_terminal_transition() -> None:
    target = _target(status=MatchStatus.VOIDED)
    plan = build_match_settlement_rollback_plan(_target())
    command = _command(plan)
    result = _committed(plan, command)
    repository = RecordingRepository(
        target,
        stored=StoredMatchSettlementRollbackOperation(
            request_fingerprint=command.request_fingerprint,
            type=MatchSettlementRollbackAuditType.ROLLED_BACK.value,
            match_id=command.match_id,
            after_data=result.to_audit_payload(),
        ),
    )
    commands, factory = _commands(repository)

    retried = commands.rollback_settlement(command)

    assert retried == result
    assert repository.calls == ["lock_match", "find_operation"]
    assert factory.created[0].commits == 1
