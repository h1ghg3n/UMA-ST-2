"""Explicit native Match result publication Application tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.match import (
    MatchResultPublicationAlreadyExistsError,
    MatchResultPublicationAuditType,
    MatchResultPublicationCommands,
    MatchResultPublicationIdempotencyConflictError,
    MatchResultPublicationInvalidSourceError,
    MatchResultPublicationLock,
    MatchResultPublicationQueries,
    MatchResultPublicationTarget,
    MatchResultPublicationTargetChoice,
    MatchResultPublicationUnavailableError,
    MatchSettlementBet,
    MatchSettlementEntry,
    MatchSettlementPayout,
    MatchSettlementRating,
    MatchSettlementResultAuthority,
    MatchSettlementReward,
    MatchSettlementRuleReference,
    MatchSettlementTarget,
    MatchSettlementWallet,
    PublishedMatchResult,
    PublishMatchResult,
    SettledMatch,
    StoredMatchResultPublication,
    StoredMatchResultPublicationOperation,
    build_match_settlement_plan,
)
from uma_st2.application.publication import (
    MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    MATCH_RESULT_CONFIRMED_EVENT_TYPE,
    MatchOpeningCondition,
    MatchPublicationDestination,
    MatchResultCourse,
    PublicationIntent,
)
from uma_st2.domain.betting import BetType
from uma_st2.domain.match import (
    MatchDirection,
    MatchGrade,
    MatchSeason,
    MatchSourceKind,
    MatchStatus,
    MatchSurface,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
    StadiumCourseLayout,
)
from uma_st2.domain.publication import PublicationStatus
from uma_st2.domain.rating import RatingRule

NOW = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _settled() -> SettledMatch:
    entries = tuple(
        MatchSettlementEntry(
            match_entry_id=10 + rank,
            entry_number=rank,
            game_account_id=100 + rank,
            owner_at_event_persona_id=f"persona-{rank}",
            game_account_name=f"trainer-{rank}",
            horse_name=f"horse-{rank}",
            affiliation_at_event="circle-a",
            rank=rank,
            rating_before=Decimal(100),
        )
        for rank in range(1, 4)
    )
    bets = (
        MatchSettlementBet(201, "persona-1", BetType.WIN, (11,), 10),
        MatchSettlementBet(202, "persona-2", BetType.QUINELLA, (11, 12), 20),
        MatchSettlementBet(203, "persona-3", BetType.TRIO, (11, 12, 13), 10),
    )
    rule_reference = MatchSettlementRuleReference(41, 3, "b" * 64)
    target = MatchSettlementTarget(
        match_id=71,
        match_name="제12회 @everyone 정기전",
        source_kind=MatchSourceKind.NATIVE_V2,
        status=MatchStatus.RESULT_CONFIRMED,
        grade=MatchGrade.G1,
        scheduled_at=SCHEDULED_AT,
        result=MatchSettlementResultAuthority(301, 2, "a" * 64),
        entries=entries,
        active_bets=bets,
        wallets=tuple(MatchSettlementWallet(f"persona-{rank}", rank * 100) for rank in range(1, 4)),
        rating_rule_version=rule_reference,
        rating_rules=tuple(
            RatingRule(MatchGrade.G1, 3, rank, Decimal(delta)) for rank, delta in enumerate(("10", "0", "-5"), start=1)
        ),
    )
    plan = build_match_settlement_plan(target)
    return SettledMatch(
        match_id=target.match_id,
        match_name=target.match_name,
        previous_status=target.status,
        status=MatchStatus.SETTLED,
        grade=target.grade,
        settled_at=NOW,
        result=target.result,
        settlement_fingerprint=plan.settlement_fingerprint,
        active_bet_ids=tuple(bet.bet_id for bet in bets),
        active_stake_total=target.active_stake_total,
        applied_odds=plan.applied_odds,
        payouts=tuple(
            MatchSettlementPayout(item.persona_id, item.bet_ids, item.amount, 1000 + index)
            for index, item in enumerate(plan.payouts, start=1)
        ),
        rewards=tuple(
            MatchSettlementReward(
                item.persona_id,
                item.selected_match_entry_id,
                item.selected_game_account_id,
                item.selected_rank,
                item.suppressed_match_entry_ids,
                item.amount,
                2000 + index,
            )
            for index, item in enumerate(plan.rewards, start=1)
        ),
        rating_rule_version=rule_reference,
        ratings=tuple(
            MatchSettlementRating(
                item.match_entry_id,
                item.entry_number,
                item.game_account_id,
                item.game_account_name,
                item.horse_name,
                item.affiliation_at_event,
                item.rank,
                item.rating_before,
                item.base_delta,
                item.adjustment_delta,
                item.amount,
                item.rating_after,
                3000 + index,
            )
            for index, item in enumerate(plan.ratings, start=1)
        ),
    )


def _target(
    *,
    source_kind: MatchSourceKind = MatchSourceKind.NATIVE_V2,
    status: MatchStatus = MatchStatus.SETTLED,
    enabled: bool = True,
    channel_id: str | None = "777777777",
) -> MatchResultPublicationTarget:
    settled = _settled()
    return MatchResultPublicationTarget(
        match_id=settled.match_id,
        match_name=settled.match_name,
        source_kind=source_kind,
        status=status,
        grade=settled.grade,
        scheduled_at=SCHEDULED_AT,
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
            announcements_enabled=enabled,
            target_channel_id=channel_id,
        ),
        settlement=settled,
    )


def _command(*, key: str = "match-result-publish:555", guild_id: str = "987654321") -> PublishMatchResult:
    return PublishMatchResult(
        match_id=71,
        idempotency_key=key,
        actor_discord_user_id="123456789",
        guild_id=guild_id,
        correlation_id="555",
    )


class RecordingRepository:
    def __init__(
        self,
        target: MatchResultPublicationTarget,
        *,
        stored_operation: StoredMatchResultPublicationOperation | None = None,
        existing_publication: StoredMatchResultPublication | None = None,
        load_error: Exception | None = None,
    ) -> None:
        self.target = target
        self.stored_operation = stored_operation
        self.existing_publication = existing_publication
        self.load_error = load_error
        self.calls: list[str] = []
        self.intent: PublicationIntent | None = None
        self.audit: tuple[MatchResultPublicationTarget, PublishedMatchResult] | None = None

    def lock_match(self, *, match_id: int) -> MatchResultPublicationLock | None:
        self.calls.append("lock_match")
        return MatchResultPublicationLock(
            self.target.match_id,
            self.target.match_name,
            self.target.source_kind,
            self.target.status,
        )

    def find_operation(self, *, idempotency_key: str) -> StoredMatchResultPublicationOperation | None:
        self.calls.append("find_operation")
        return self.stored_operation

    def find_result_publication(self, *, match_id: int) -> StoredMatchResultPublication | None:
        self.calls.append("find_result_publication")
        return self.existing_publication

    def load_target(self, *, match_id: int, guild_id: str) -> MatchResultPublicationTarget | None:
        self.calls.append("load_target")
        if self.load_error is not None:
            raise self.load_error
        return self.target

    def add_publication(
        self,
        *,
        intent: PublicationIntent,
        created_at: datetime,
    ) -> StoredMatchResultPublication:
        self.calls.append("add_publication")
        self.intent = intent
        return StoredMatchResultPublication(
            publication_id=901,
            event_key=intent.event_key,
            payload_fingerprint=intent.payload_fingerprint,
            status=intent.status,
            target_channel_id=intent.target_channel_id,
        )

    def add_audit(
        self,
        *,
        command: PublishMatchResult,
        before: MatchResultPublicationTarget,
        after: PublishedMatchResult,
        created_at: datetime,
    ) -> None:
        self.calls.append("add_audit")
        assert command.match_id == before.match_id
        assert created_at == NOW
        self.audit = (before, after)

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchResultPublicationTargetChoice, ...]:
        self.calls.append(f"search_targets:{search}:{limit}")
        return (MatchResultPublicationTargetChoice(self.target.match_id, self.target.match_name, self.target.grade),)


@dataclass
class RecordingUnitOfWork:
    match_result_publication: RecordingRepository
    commits: int = 0
    rollbacks: int = 0

    @property
    def match_result_publication_queries(self) -> RecordingRepository:
        return self.match_result_publication

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


def _commands(repository: RecordingRepository) -> tuple[MatchResultPublicationCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return MatchResultPublicationCommands(CommandRunner(factory), clock=lambda: NOW), factory


def test_publish_creates_public_safe_settlement_snapshot_and_audit() -> None:
    repository = RecordingRepository(_target())
    commands, factory = _commands(repository)

    result = commands.publish_result(_command())

    assert result.match_status is MatchStatus.SETTLED
    assert result.publication.status is PublicationStatus.READY
    assert repository.calls == [
        "lock_match",
        "find_operation",
        "find_result_publication",
        "load_target",
        "add_publication",
        "add_audit",
    ]
    assert factory.created[0].commits == 1
    assert repository.intent is not None
    assert repository.intent.destination_kind == MATCH_ANNOUNCEMENT_DESTINATION_KIND
    assert repository.intent.event_type == MATCH_RESULT_CONFIRMED_EVENT_TYPE
    payload = repository.intent.payload_json
    assert payload["publication_type"] == "match_settled_result"
    assert payload["results"][0] == {  # type: ignore[index]
        "entry_number": 1,
        "official_rank": 1,
        "player_name": "trainer-1",
        "character_name": "horse-1",
        "affiliation": "circle-a",
        "rating_disposition": "rated",
        "rating_rank": 1,
        "rating": {"before": "100.0000", "delta": "10.0000", "after": "110.0000"},
    }
    assert payload["odds"]["markets"][0]["confirmed_odds"] == "0.6"  # type: ignore[index]
    serialized = str(payload)
    for forbidden in (
        "game_account_id",
        "match_entry_id",
        "persona_id",
        "active_bet",
        "stake",
        "pool",
        "payout",
        "actor",
        "request",
        "reason",
        "uma_pid",
    ):
        assert forbidden not in serialized
    assert repository.audit is not None


@pytest.mark.parametrize(
    ("enabled", "channel_id", "expected_status"),
    (
        (False, "777777777", PublicationStatus.SUPPRESSED),
        (True, None, PublicationStatus.AWAITING_CHANNEL),
        (True, "777777777", PublicationStatus.READY),
    ),
)
def test_current_destination_setting_selects_initial_status(
    enabled: bool,
    channel_id: str | None,
    expected_status: PublicationStatus,
) -> None:
    repository = RecordingRepository(_target(enabled=enabled, channel_id=channel_id))
    commands, _ = _commands(repository)

    result = commands.publish_result(_command())

    assert result.publication.status is expected_status


@pytest.mark.parametrize(
    ("source_kind", "status"),
    (
        (MatchSourceKind.IMPORTED_V1, MatchStatus.SETTLED),
        (MatchSourceKind.NATIVE_V2, MatchStatus.RESULT_CONFIRMED),
        (MatchSourceKind.NATIVE_V2, MatchStatus.VOIDED),
    ),
)
def test_non_native_or_unsettled_match_is_zero_write(
    source_kind: MatchSourceKind,
    status: MatchStatus,
) -> None:
    repository = RecordingRepository(_target(source_kind=source_kind, status=status))
    commands, factory = _commands(repository)

    with pytest.raises(MatchResultPublicationUnavailableError):
        commands.publish_result(_command())

    assert repository.calls == ["lock_match", "find_operation"]
    assert factory.created[0].rollbacks == 1


def test_second_logical_publication_is_rejected_without_write() -> None:
    existing = StoredMatchResultPublication(
        publication_id=900,
        event_key="match:71:settled-result:v1",
        payload_fingerprint="f" * 64,
        status=PublicationStatus.SENT,
        target_channel_id="777777777",
    )
    repository = RecordingRepository(_target(), existing_publication=existing)
    commands, factory = _commands(repository)

    with pytest.raises(MatchResultPublicationAlreadyExistsError):
        commands.publish_result(_command(key="match-result-publish:556"))

    assert "add_publication" not in repository.calls
    assert factory.created[0].rollbacks == 1


def test_exact_retry_returns_stored_receipt_and_changed_guild_conflicts() -> None:
    initial_repository = RecordingRepository(_target())
    initial_commands, _ = _commands(initial_repository)
    command = _command()
    committed = initial_commands.publish_result(command)
    stored = StoredMatchResultPublicationOperation(
        request_fingerprint=command.request_fingerprint,
        type=MatchResultPublicationAuditType.PUBLISHED.value,
        match_id=command.match_id,
        after_data=committed.to_audit_payload(),
    )
    repository = RecordingRepository(_target(), stored_operation=stored)
    commands, _ = _commands(repository)

    assert commands.publish_result(command) == committed
    assert repository.calls == ["lock_match", "find_operation"]

    conflict_repository = RecordingRepository(_target(), stored_operation=stored)
    conflict_commands, _ = _commands(conflict_repository)
    with pytest.raises(MatchResultPublicationIdempotencyConflictError):
        conflict_commands.publish_result(_command(guild_id="111111111"))


def test_malformed_settlement_authority_is_zero_write() -> None:
    repository = RecordingRepository(_target(), load_error=ValueError("malformed audit"))
    commands, factory = _commands(repository)

    with pytest.raises(MatchResultPublicationInvalidSourceError):
        commands.publish_result(_command())

    assert "add_publication" not in repository.calls
    assert factory.created[0].rollbacks == 1


def test_publishable_target_query_uses_read_only_uow() -> None:
    repository = RecordingRepository(_target())
    factory = RecordingFactory(repository)
    queries = MatchResultPublicationQueries(QueryRunner(factory))

    choices = queries.search_targets(search="정기", limit=10)

    assert choices[0].match_id == 71
    assert repository.calls == ["search_targets:정기:10"]
    assert factory.created[0].commits == 0
    assert factory.created[0].rollbacks == 1
