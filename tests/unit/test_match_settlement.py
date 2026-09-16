"""Native V2 Match settlement Application tests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.match import (
    MatchResultPublicationTarget,
    MatchSettlementAuditError,
    MatchSettlementAuditType,
    MatchSettlementBet,
    MatchSettlementCommands,
    MatchSettlementEntry,
    MatchSettlementIdempotencyConflictError,
    MatchSettlementInvalidSourceError,
    MatchSettlementLock,
    MatchSettlementPayout,
    MatchSettlementQueries,
    MatchSettlementRating,
    MatchSettlementRatingSelectionError,
    MatchSettlementResultAuthority,
    MatchSettlementReward,
    MatchSettlementRuleReference,
    MatchSettlementRuleUnavailableError,
    MatchSettlementStaleError,
    MatchSettlementTarget,
    MatchSettlementTargetChoice,
    MatchSettlementWallet,
    SettledMatch,
    SettleMatch,
    StoredMatchSettlementOperation,
    build_match_settlement_plan,
)
from uma_st2.application.publication import (
    MATCH_RESULT_CONFIRMED_EVENT_TYPE,
    MatchOpeningCondition,
    MatchPublicationDestination,
    MatchResultCourse,
    MatchResultPublicationSource,
    PublicationIntent,
)
from uma_st2.domain.betting import BetType
from uma_st2.domain.match import (
    MatchDirection,
    MatchGrade,
    MatchRatingDisposition,
    MatchSeason,
    MatchSourceKind,
    MatchStatus,
    MatchSurface,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
    StadiumCourseLayout,
    calculate_match_placement_reward,
)
from uma_st2.domain.rating import RatingRule

NOW = datetime(2026, 8, 28, 13, 0, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
ZERO = Decimal("0.000000000000000000")


def _rules(
    grade: MatchGrade = MatchGrade.G1,
    *,
    participant_count: int = 3,
) -> tuple[RatingRule, ...]:
    deltas = ("10", "0", "-5")[:participant_count]
    return tuple(
        RatingRule(grade, participant_count, rank, Decimal(delta)) for rank, delta in enumerate(deltas, start=1)
    )


def _target(
    *,
    grade: MatchGrade = MatchGrade.G1,
    same_persona: bool = False,
    rating_rules: tuple[RatingRule, ...] | None = None,
) -> MatchSettlementTarget:
    owners = ("persona-a", "persona-b", "persona-a" if same_persona else "persona-c")
    entries = tuple(
        MatchSettlementEntry(
            match_entry_id=10 + rank,
            entry_number=rank,
            game_account_id=100 + rank,
            owner_at_event_persona_id=owners[rank - 1],
            game_account_name=f"trainer-{rank}",
            horse_name=f"horse-{rank}",
            affiliation_at_event="circle-a",
            rank=rank,
            rating_before=Decimal(100),
        )
        for rank in range(1, 4)
    )
    bets = (
        MatchSettlementBet(201, "persona-a", BetType.WIN, (11,), 10),
        MatchSettlementBet(202, "persona-b", BetType.QUINELLA, (11, 12), 20),
        MatchSettlementBet(203, "persona-c", BetType.TRIO, (11, 12, 13), 10),
        MatchSettlementBet(204, "persona-c", BetType.WIN, (12,), 10),
    )
    required_personas = sorted(set(owners) | {bet.persona_id for bet in bets})
    rule_reference = None if grade is MatchGrade.OP else MatchSettlementRuleReference(41, 3, "b" * 64)
    if rating_rules is None:
        rating_rules = () if grade in (MatchGrade.LISTED, MatchGrade.OP) else _rules(grade)
    return MatchSettlementTarget(
        match_id=71,
        match_name="제12회 정기전",
        source_kind=MatchSourceKind.NATIVE_V2,
        status=MatchStatus.RESULT_CONFIRMED,
        grade=grade,
        scheduled_at=SCHEDULED_AT,
        result=MatchSettlementResultAuthority(301, 2, "a" * 64),
        entries=entries,
        active_bets=bets,
        wallets=tuple(
            MatchSettlementWallet(persona_id, (index + 1) * 100) for index, persona_id in enumerate(required_personas)
        ),
        rating_rule_version=rule_reference,
        rating_rules=rating_rules,
    )


def _committed(
    target: MatchSettlementTarget,
    *,
    excluded_rating_entry_ids: tuple[int, ...] = (),
) -> SettledMatch:
    plan = build_match_settlement_plan(
        target,
        excluded_rating_entry_ids=excluded_rating_entry_ids,
    )
    return SettledMatch(
        match_id=target.match_id,
        match_name=target.match_name,
        previous_status=target.status,
        status=MatchStatus.SETTLED,
        grade=target.grade,
        settled_at=NOW,
        result=target.result,
        settlement_fingerprint=plan.settlement_fingerprint,
        active_bet_ids=tuple(bet.bet_id for bet in target.active_bets),
        active_stake_total=target.active_stake_total,
        applied_odds=plan.applied_odds,
        payouts=tuple(
            MatchSettlementPayout(
                payout.persona_id,
                payout.bet_ids,
                payout.amount,
                1000 + index,
            )
            for index, payout in enumerate(plan.payouts, start=1)
        ),
        rewards=tuple(
            MatchSettlementReward(
                reward.persona_id,
                reward.selected_match_entry_id,
                reward.selected_game_account_id,
                reward.selected_rank,
                reward.suppressed_match_entry_ids,
                reward.amount,
                2000 + index,
            )
            for index, reward in enumerate(plan.rewards, start=1)
        ),
        rating_rule_version=target.rating_rule_version,
        ratings=tuple(
            MatchSettlementRating(
                rating.match_entry_id,
                rating.entry_number,
                rating.game_account_id,
                rating.game_account_name,
                rating.horse_name,
                rating.affiliation_at_event,
                rating.rank,
                rating.rating_before,
                rating.base_delta,
                rating.adjustment_delta,
                rating.amount,
                rating.rating_after,
                3000 + index if rating.transaction_required else None,
                rating.rating_disposition,
                rating.rating_rank,
            )
            for index, rating in enumerate(plan.ratings, start=1)
        ),
    )


def _publication_source(target: MatchSettlementTarget) -> MatchResultPublicationSource:
    settled = _committed(target)
    return MatchResultPublicationTarget(
        match_id=settled.match_id,
        match_name=settled.match_name,
        source_kind=MatchSourceKind.NATIVE_V2,
        status=MatchStatus.SETTLED,
        grade=settled.grade,
        scheduled_at=target.scheduled_at,
        course=MatchResultCourse(
            stadium_name="도쿄",
            surface=MatchSurface.TURF,
            distance=2400,
            direction=MatchDirection.LEFT,
            layout=StadiumCourseLayout.STANDARD,
        ),
        condition=MatchOpeningCondition(
            season=MatchSeason.AUTUMN,
            weather=MatchWeather.SUNNY,
            time_of_day=MatchTimeOfDay.NIGHT,
            track_condition=MatchTrackCondition.FIRM,
        ),
        destination=MatchPublicationDestination(
            guild_id="987654321",
            announcements_enabled=True,
            target_channel_id="777777777",
        ),
        settlement=settled,
    ).to_publication_source()


def _command(
    target: MatchSettlementTarget,
    *,
    reason: str | None = None,
    excluded_rating_entry_ids: tuple[int, ...] = (),
) -> SettleMatch:
    return SettleMatch(
        match_id=target.match_id,
        expected_settlement_fingerprint=target.settlement_fingerprint(
            excluded_rating_entry_ids=excluded_rating_entry_ids,
        ),
        excluded_rating_entry_ids=excluded_rating_entry_ids,
        idempotency_key="match-settlement:555",
        actor_discord_user_id="123456789",
        guild_id="987654321",
        reason=reason,
        correlation_id="555",
    )


class RecordingRepository:
    def __init__(
        self,
        target: MatchSettlementTarget,
        *,
        stored: StoredMatchSettlementOperation | None = None,
        publication_source_available: bool = True,
    ) -> None:
        self.target = target
        self.stored = stored
        self.publication_source_available = publication_source_available
        self.calls: list[str] = []
        self.publication_intents: list[PublicationIntent] = []

    def lock_match(self, *, match_id: int) -> MatchSettlementLock | None:
        self.calls.append("lock_match")
        assert match_id == self.target.match_id
        return MatchSettlementLock(
            self.target.match_id,
            self.target.match_name,
            self.target.source_kind,
            self.target.status,
        )

    def find_operation(self, *, idempotency_key: str) -> StoredMatchSettlementOperation | None:
        self.calls.append("find_operation")
        return self.stored

    def load_target(self, *, match_id: int, lock: bool = False) -> MatchSettlementTarget | None:
        self.calls.append(f"load_target:{lock}")
        return self.target

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchSettlementTargetChoice, ...]:
        self.calls.append(f"search_targets:{search}:{limit}")
        return (
            MatchSettlementTargetChoice(
                self.target.match_id,
                self.target.match_name,
                len(self.target.entries),
                len(self.target.active_bets),
            ),
        )

    def persist_settlement(self, *, command: SettleMatch, plan: object, settled_at: datetime) -> SettledMatch:
        self.calls.append("persist_settlement")
        assert command.match_id == self.target.match_id
        assert settled_at == NOW
        return _committed(
            self.target,
            excluded_rating_entry_ids=command.excluded_rating_entry_ids,
        )

    def load_result_publication_source(
        self,
        *,
        match_id: int,
        guild_id: str,
    ) -> MatchResultPublicationSource | None:
        self.calls.append("load_result_publication_source")
        assert match_id == self.target.match_id
        assert guild_id == "987654321"
        return _publication_source(self.target) if self.publication_source_available else None

    def add_result_publication(
        self,
        *,
        intent: PublicationIntent,
        created_at: datetime,
    ) -> None:
        self.calls.append("add_result_publication")
        assert created_at == NOW
        self.publication_intents.append(intent)


@dataclass
class RecordingUnitOfWork:
    match_settlement: RecordingRepository
    commits: int = 0
    rollbacks: int = 0

    @property
    def match_settlement_queries(self) -> RecordingRepository:
        return self.match_settlement

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


def test_placement_reward_table_and_small_field_multiplier() -> None:
    assert calculate_match_placement_reward(grade=MatchGrade.G1, rank=1, field_size=9) == 300
    assert calculate_match_placement_reward(grade=MatchGrade.G2, rank=2, field_size=9) == 160
    assert calculate_match_placement_reward(grade=MatchGrade.LISTED, rank=3, field_size=9) == 100
    assert calculate_match_placement_reward(grade=MatchGrade.G3, rank=5, field_size=8) == 40
    assert calculate_match_placement_reward(grade=MatchGrade.OP, rank=1, field_size=3) == 0


def test_g1_plan_applies_winning_odds_payout_rewards_rating_and_wallet_deltas() -> None:
    plan = build_match_settlement_plan(_target())

    assert tuple((item.bet_type, item.provisional_odds, item.confirmed_odds) for item in plan.applied_odds) == (
        (BetType.WIN, Decimal("2.0910"), Decimal("1.1")),
        (BetType.QUINELLA, Decimal("1.2693"), Decimal("0.7")),
        (BetType.TRIO, Decimal("1.0000"), Decimal("0.5")),
    )
    assert tuple((item.persona_id, item.bet_ids, item.amount) for item in plan.payouts) == (
        ("persona-a", (201,), 11),
        ("persona-b", (202,), 14),
        ("persona-c", (203,), 5),
    )
    assert tuple((item.persona_id, item.selected_rank, item.amount) for item in plan.rewards) == (
        ("persona-a", 1, 150),
        ("persona-b", 2, 100),
        ("persona-c", 3, 70),
    )
    assert tuple(item.amount for item in plan.ratings) == (
        Decimal("10.000000000000000000"),
        ZERO,
        Decimal("-5.000000000000000000"),
    )
    assert tuple(
        (item.persona_id, item.payout_amount, item.reward_amount, item.balance_after) for item in plan.wallet_deltas
    ) == (
        ("persona-a", 11, 150, 261),
        ("persona-b", 14, 100, 314),
        ("persona-c", 5, 70, 375),
    )


def test_operator_exclusion_uses_full_official_board_and_reorders_only_rating() -> None:
    target = _target(rating_rules=_rules() + _rules(participant_count=2))
    duplicate_account_entry = replace(
        target.entries[1],
        game_account_id=target.entries[0].game_account_id,
        game_account_name=target.entries[0].game_account_name,
        rating_before=target.entries[0].rating_before,
    )
    target = replace(target, entries=(target.entries[0], duplicate_account_entry, target.entries[2]))

    with pytest.raises(MatchSettlementRatingSelectionError, match="at most one rated Entry"):
        build_match_settlement_plan(target)

    plan = build_match_settlement_plan(target, excluded_rating_entry_ids=(12,))

    assert plan.excluded_rating_entry_ids == (12,)
    assert tuple((item.rank, item.rating_disposition, item.rating_rank) for item in plan.ratings) == (
        (1, MatchRatingDisposition.RATED, 1),
        (2, MatchRatingDisposition.EXCLUDED, None),
        (3, MatchRatingDisposition.RATED, 2),
    )
    assert plan.ratings[1].amount == ZERO
    assert plan.ratings[1].rating_after == plan.ratings[1].rating_before
    assert tuple(item.selection_entry_numbers for item in plan.applied_odds) == ((1,), (1, 2), (1, 2, 3))
    assert tuple(item.selected_rank for item in plan.rewards) == (1, 2, 3)


def test_all_rating_entries_may_be_excluded_but_exactly_one_may_not_remain() -> None:
    target = _target()

    all_excluded = build_match_settlement_plan(target, excluded_rating_entry_ids=(11, 12, 13))

    assert all_excluded.excluded_rating_entry_ids == (11, 12, 13)
    assert all(item.rating_disposition is MatchRatingDisposition.EXCLUDED for item in all_excluded.ratings)
    assert all(not item.transaction_required and item.amount == ZERO for item in all_excluded.ratings)
    assert len(all_excluded.rewards) == 3

    with pytest.raises(MatchSettlementRatingSelectionError, match="zero or at least two"):
        build_match_settlement_plan(target, excluded_rating_entry_ids=(11, 12))


def test_rating_exclusion_selection_must_be_unique_current_entry_ids_and_op_is_not_applicable() -> None:
    target = _target()

    with pytest.raises(MatchSettlementRatingSelectionError, match="belong to the current official board"):
        build_match_settlement_plan(target, excluded_rating_entry_ids=(999,))
    with pytest.raises(MatchSettlementRatingSelectionError, match="must be unique"):
        build_match_settlement_plan(target, excluded_rating_entry_ids=(11, 11))

    op_plan = build_match_settlement_plan(_target(grade=MatchGrade.OP))
    assert all(item.rating_disposition is MatchRatingDisposition.NOT_APPLICABLE for item in op_plan.ratings)
    with pytest.raises(MatchSettlementRatingSelectionError, match="already Rating-not-applicable"):
        build_match_settlement_plan(_target(grade=MatchGrade.OP), excluded_rating_entry_ids=(11,))


def test_same_persona_receives_only_highest_placement_reward_with_suppressed_provenance() -> None:
    plan = build_match_settlement_plan(_target(same_persona=True))

    reward = next(item for item in plan.rewards if item.persona_id == "persona-a")
    assert reward.selected_match_entry_id == 11
    assert reward.selected_rank == 1
    assert reward.suppressed_match_entry_ids == (13,)
    assert reward.amount == 150


def test_op_has_no_reward_rule_or_rating_transaction() -> None:
    target = _target(grade=MatchGrade.OP)
    plan = build_match_settlement_plan(target)
    committed = _committed(target)

    assert plan.rewards == ()
    assert all(not rating.transaction_required and rating.amount == ZERO for rating in plan.ratings)
    assert committed.rating_rule_version is None
    assert committed.rating_transaction_ids == ()
    assert SettledMatch.from_audit_payload(committed.to_audit_payload()) == committed


def test_incomplete_rating_rule_version_is_rejected_before_write() -> None:
    target = _target(rating_rules=_rules()[:2])

    with pytest.raises(MatchSettlementRuleUnavailableError, match="cover converted ranks"):
        build_match_settlement_plan(target)


def test_command_recomputes_locked_plan_and_commits_once() -> None:
    target = _target()
    repository = RecordingRepository(target)
    factory = RecordingFactory(repository)
    commands = MatchSettlementCommands(CommandRunner(factory), clock=lambda: NOW)

    result = commands.settle_match(_command(target, reason="운영 확정"))

    assert result == _committed(target)
    assert repository.calls == [
        "lock_match",
        "find_operation",
        "load_target:True",
        "persist_settlement",
        "load_result_publication_source",
        "add_result_publication",
    ]
    assert len(repository.publication_intents) == 1
    assert repository.publication_intents[0].event_type == MATCH_RESULT_CONFIRMED_EVENT_TYPE
    assert repository.publication_intents[0].source_id == target.match_id
    assert factory.created[0].commits == 1


def test_command_binds_exact_rating_selection_to_fingerprint_and_receipt() -> None:
    target = _target(rating_rules=_rules() + _rules(participant_count=2))
    repository = RecordingRepository(target)
    factory = RecordingFactory(repository)
    commands = MatchSettlementCommands(CommandRunner(factory), clock=lambda: NOW)
    command = _command(target, excluded_rating_entry_ids=(13,))

    result = commands.settle_match(command)

    assert result.excluded_rating_entry_ids == (13,)
    assert len(result.rating_transaction_ids) == 2
    assert command.request_fingerprint != _command(target).request_fingerprint
    assert factory.created[0].commits == 1


def test_stale_preview_is_zero_write() -> None:
    target = _target()
    repository = RecordingRepository(target)
    factory = RecordingFactory(repository)
    commands = MatchSettlementCommands(CommandRunner(factory), clock=lambda: NOW)
    stale = SettleMatch(
        match_id=target.match_id,
        expected_settlement_fingerprint="f" * 64,
        excluded_rating_entry_ids=(),
        idempotency_key="match-settlement:555",
        actor_discord_user_id="123456789",
        guild_id="987654321",
    )

    with pytest.raises(MatchSettlementStaleError):
        commands.settle_match(stale)

    assert "persist_settlement" not in repository.calls
    assert factory.created[0].rollbacks == 1


def test_missing_publication_source_rolls_back_settlement_uow() -> None:
    target = _target()
    repository = RecordingRepository(target, publication_source_available=False)
    factory = RecordingFactory(repository)
    commands = MatchSettlementCommands(CommandRunner(factory), clock=lambda: NOW)

    with pytest.raises(MatchSettlementInvalidSourceError, match="publication evidence"):
        commands.settle_match(_command(target))

    assert repository.calls[-2:] == ["persist_settlement", "load_result_publication_source"]
    assert repository.publication_intents == []
    assert factory.created[0].commits == 0
    assert factory.created[0].rollbacks == 1


def test_exact_retry_returns_complete_stored_receipt_and_changed_reason_conflicts() -> None:
    target = _target()
    command = _command(target)
    committed = _committed(target)
    stored = StoredMatchSettlementOperation(
        request_fingerprint=command.request_fingerprint,
        type=MatchSettlementAuditType.SETTLED.value,
        match_id=target.match_id,
        after_data=committed.to_audit_payload(),
    )
    repository = RecordingRepository(target, stored=stored)
    factory = RecordingFactory(repository)
    commands = MatchSettlementCommands(CommandRunner(factory), clock=lambda: NOW)

    assert commands.settle_match(command) == committed
    assert repository.calls == ["lock_match", "find_operation"]

    conflict_repository = RecordingRepository(target, stored=stored)
    conflict_commands = MatchSettlementCommands(
        CommandRunner(RecordingFactory(conflict_repository)),
        clock=lambda: NOW,
    )
    with pytest.raises(MatchSettlementIdempotencyConflictError):
        conflict_commands.settle_match(_command(target, reason="다른 사유"))


def test_exact_retry_rejects_non_object_evidence_items() -> None:
    target = _target()
    command = _command(target)
    payload = _committed(target).to_audit_payload()
    payload["ratings"] = [*payload["ratings"], "malformed"]
    stored = StoredMatchSettlementOperation(
        request_fingerprint=command.request_fingerprint,
        type=MatchSettlementAuditType.SETTLED.value,
        match_id=target.match_id,
        after_data=payload,
    )
    repository = RecordingRepository(target, stored=stored)
    commands = MatchSettlementCommands(CommandRunner(RecordingFactory(repository)), clock=lambda: NOW)

    with pytest.raises(MatchSettlementAuditError):
        commands.settle_match(command)


def test_exact_retry_rejects_odds_for_a_non_winning_entry() -> None:
    target = _target()
    command = _command(target)
    payload = _committed(target).to_audit_payload()
    win_market = payload["odds"]["markets"][0]
    win_market["selection_entry_ids"] = [target.entries[1].match_entry_id]
    win_market["selection_entry_numbers"] = [target.entries[1].entry_number]
    stored = StoredMatchSettlementOperation(
        request_fingerprint=command.request_fingerprint,
        type=MatchSettlementAuditType.SETTLED.value,
        match_id=target.match_id,
        after_data=payload,
    )
    repository = RecordingRepository(target, stored=stored)
    commands = MatchSettlementCommands(CommandRunner(RecordingFactory(repository)), clock=lambda: NOW)

    with pytest.raises(MatchSettlementAuditError):
        commands.settle_match(command)


def test_queries_return_preview_without_committing() -> None:
    target = _target()
    repository = RecordingRepository(target)
    factory = RecordingFactory(repository)
    queries = MatchSettlementQueries(QueryRunner(factory))

    choices = queries.search_targets(search="정기", limit=10)
    selection = queries.get_rating_selection(match_id=target.match_id)
    preview = queries.get_preview(match_id=target.match_id)
    selected_preview = MatchSettlementQueries(QueryRunner(factory)).get_preview(
        match_id=target.match_id,
        excluded_rating_entry_ids=(11, 12, 13),
    )

    assert choices[0].match_id == target.match_id
    assert tuple(item.match_entry_id for item in selection.entries) == (11, 12, 13)
    assert preview.target == target
    assert selected_preview.excluded_rating_entry_ids == (11, 12, 13)
    assert all(unit_of_work.commits == 0 and unit_of_work.rollbacks == 1 for unit_of_work in factory.created)


def test_settlement_audit_v1_reader_infers_legacy_rating_participation() -> None:
    committed = _committed(_target())
    payload = committed.to_audit_payload()
    payload["schema_version"] = 1
    payload.pop("excluded_rating_entry_ids")
    for rating in payload["ratings"]:
        rating.pop("rating_disposition")
        rating.pop("rating_rank")

    restored = SettledMatch.from_audit_payload(payload)

    assert restored == committed
