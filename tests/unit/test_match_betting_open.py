"""Native Match betting-open Application and publication tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner
from uma_st2.application.match import (
    MATCH_BETTING_OPEN_MINIMUM_ENTRY_COUNT,
    MatchBettingOpenAuditType,
    MatchBettingOpenCommands,
    MatchBettingOpenIncompleteError,
    MatchBettingOpenInvalidSourceError,
    MatchBettingOpenRatingRuleCoverage,
    MatchBettingOpenStaleError,
    MatchBettingOpenTarget,
    MatchBettingOpenTargetChoice,
    OpenedMatchBetting,
    OpenMatchBetting,
    StoredMatchBettingOpenOperation,
    StoredMatchOpeningPublication,
)
from uma_st2.application.publication import (
    MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    MatchOpeningCondition,
    MatchOpeningCourse,
    MatchOpeningEntry,
    MatchPublicationDestination,
    PublicationIntent,
)
from uma_st2.domain.match import (
    MATCH_ENTRY_MAXIMUM_COUNT,
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

NOW = datetime(2026, 8, 28, 7, 0, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
DEFAULT_CONDITION = MatchOpeningCondition(
    MatchSeason.AUTUMN,
    MatchWeather.SUNNY,
    MatchTimeOfDay.DAY,
    MatchTrackCondition.FIRM,
)


def _target(
    *,
    status: MatchStatus = MatchStatus.SCHEDULED,
    condition: MatchOpeningCondition | None = DEFAULT_CONDITION,
    entry_count: int = 3,
    enabled: bool = True,
    channel_id: str | None = "777777777",
    grade: MatchGrade = MatchGrade.G1,
    current_rule_version_available: bool = True,
    current_rule_version_complete: bool = True,
    covered_converted_ranks: tuple[int, ...] | None = None,
) -> MatchBettingOpenTarget:
    if covered_converted_ranks is None:
        covered_converted_ranks = (
            tuple(range(1, entry_count + 1))
            if grade
            in {
                MatchGrade.G1,
                MatchGrade.G2,
                MatchGrade.G3,
            }
            else ()
        )
    return MatchBettingOpenTarget(
        match_id=71,
        match_name="제12회 @everyone 정기전",
        description="공식 룸매치",
        source_kind=MatchSourceKind.NATIVE_V2,
        status=status,
        grade=grade,
        scheduled_at=SCHEDULED_AT,
        course=MatchOpeningCourse(
            course_id=11,
            stadium_id=5,
            stadium_name="도쿄",
            surface=MatchSurface.TURF,
            distance=2400,
            direction=MatchDirection.LEFT,
            layout=StadiumCourseLayout.STANDARD,
        ),
        condition=condition,
        rating_rule_coverage=MatchBettingOpenRatingRuleCoverage(
            current_version_available=current_rule_version_available,
            current_version_complete=current_rule_version_complete,
            covered_converted_ranks=covered_converted_ranks,
        ),
        entries=tuple(
            MatchOpeningEntry(
                entry_id=100 + index,
                entry_number=index,
                game_account_name=f"계정 {index}",
                horse_name=f"말 {index}",
                affiliation="A조",
            )
            for index in range(1, entry_count + 1)
        ),
        destination=MatchPublicationDestination(
            guild_id="987654321",
            announcements_enabled=enabled,
            target_channel_id=channel_id,
        ),
    )


def _command(target: MatchBettingOpenTarget, *, key: str = "match-betting-open:555") -> OpenMatchBetting:
    return OpenMatchBetting(
        match_id=target.match_id,
        expected_state_fingerprint=target.state_fingerprint,
        idempotency_key=key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id="555",
    )


class RecordingRepository:
    def __init__(
        self,
        target: MatchBettingOpenTarget,
        *,
        stored: StoredMatchBettingOpenOperation | None = None,
        has_bet: bool = False,
    ) -> None:
        self.target = target
        self.stored = stored
        self.has_bet = has_bet
        self.calls: list[str] = []
        self.intent: PublicationIntent | None = None
        self.audit: tuple[MatchBettingOpenTarget, OpenedMatchBetting] | None = None

    def lock_target(self, *, match_id: int, guild_id: str) -> MatchBettingOpenTarget | None:
        self.calls.append("lock_target")
        return self.target

    def find_operation(self, *, idempotency_key: str) -> StoredMatchBettingOpenOperation | None:
        self.calls.append("find_operation")
        return self.stored

    def has_any_bet_facts(self, *, match_id: int) -> bool:
        self.calls.append("has_any_bet_facts")
        return self.has_bet

    def transition_to_betting_open(self, *, match_id: int, changed_at: datetime) -> None:
        self.calls.append("transition_to_betting_open")

    def start_periodic_odds_cycle_if_first_open(
        self,
        *,
        match_id: int,
        guild_id: str,
        opened_at: datetime,
    ) -> None:
        self.calls.append("start_periodic_odds_cycle_if_first_open")
        assert match_id == self.target.match_id
        assert guild_id == "987654321"
        assert opened_at == NOW

    def add_publication(
        self,
        *,
        intent: PublicationIntent,
        created_at: datetime,
    ) -> StoredMatchOpeningPublication:
        self.calls.append("add_publication")
        self.intent = intent
        return StoredMatchOpeningPublication(
            publication_id=901,
            event_key=intent.event_key,
            payload_fingerprint=intent.payload_fingerprint,
            status=intent.status,
            target_channel_id=intent.target_channel_id,
        )

    def add_audit(
        self,
        *,
        command: OpenMatchBetting,
        before: MatchBettingOpenTarget,
        after: OpenedMatchBetting,
        created_at: datetime,
    ) -> None:
        self.calls.append("add_audit")
        assert command.match_id == 71
        assert created_at == NOW
        self.audit = (before, after)


@dataclass
class RecordingUnitOfWork:
    match_betting_open: RecordingRepository
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


def _commands(repository: RecordingRepository) -> tuple[MatchBettingOpenCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return MatchBettingOpenCommands(CommandRunner(factory), clock=lambda: NOW), factory


def test_open_transition_audit_and_publication_are_one_command_result() -> None:
    target = _target()
    repository = RecordingRepository(target)
    commands, factory = _commands(repository)

    result = commands.open_betting(_command(target))

    assert result.status == MatchStatus.BETTING_OPEN
    assert result.entry_count == 3
    assert result.publication.status == PublicationStatus.READY
    assert repository.calls == [
        "lock_target",
        "find_operation",
        "has_any_bet_facts",
        "transition_to_betting_open",
        "start_periodic_odds_cycle_if_first_open",
        "add_publication",
        "add_audit",
    ]
    assert factory.created[0].commits == 1
    assert repository.intent is not None
    assert repository.intent.destination_kind == MATCH_ANNOUNCEMENT_DESTINATION_KIND
    payload = repository.intent.payload_json
    assert payload["entries"] == [entry.to_payload() for entry in target.entries]
    assert payload["odds"]["zero_pool"] is True  # type: ignore[index]
    assert [market["bet_type"] for market in payload["odds"]["markets"]] == [  # type: ignore[index]
        "win",
        "quinella",
        "trio",
    ]
    assert all(key not in str(payload) for key in ("participant_count", "pool_amount", "persona_id"))
    assert "rating_rule_coverage" not in target.to_audit_payload()
    assert "rating_rule_coverage" not in payload


def test_two_to_eighteen_entries_are_required_for_opening_readiness_and_markets() -> None:
    one_entry = _target(entry_count=1)
    one_entry_choice = MatchBettingOpenTargetChoice(
        match_id=one_entry.match_id,
        match_name=one_entry.match_name,
        grade=one_entry.grade,
        entry_count=len(one_entry.entries),
        has_complete_condition=True,
        rating_rule_coverage=one_entry.rating_rule_coverage,
    )

    assert MATCH_BETTING_OPEN_MINIMUM_ENTRY_COUNT == 2
    assert one_entry.readiness_issues == ("Entry가 최소 2명 필요합니다. (현재 1명)",)
    assert one_entry.markets == ()
    assert one_entry_choice.is_ready is False

    two_entries = _target(entry_count=2)
    two_entry_choice = MatchBettingOpenTargetChoice(
        match_id=two_entries.match_id,
        match_name=two_entries.match_name,
        grade=two_entries.grade,
        entry_count=len(two_entries.entries),
        has_complete_condition=True,
        rating_rule_coverage=two_entries.rating_rule_coverage,
    )

    assert two_entries.readiness_issues == ()
    assert [market.available for market in two_entries.markets] == [True, True, False]
    assert two_entry_choice.is_ready is True

    maximum_entries = _target(entry_count=MATCH_ENTRY_MAXIMUM_COUNT)
    maximum_entry_choice = MatchBettingOpenTargetChoice(
        match_id=maximum_entries.match_id,
        match_name=maximum_entries.match_name,
        grade=maximum_entries.grade,
        entry_count=len(maximum_entries.entries),
        has_complete_condition=True,
        rating_rule_coverage=maximum_entries.rating_rule_coverage,
    )
    assert maximum_entries.readiness_issues == ()
    assert maximum_entry_choice.is_ready is True

    too_many_entries = _target(entry_count=MATCH_ENTRY_MAXIMUM_COUNT + 1)
    too_many_entry_choice = MatchBettingOpenTargetChoice(
        match_id=too_many_entries.match_id,
        match_name=too_many_entries.match_name,
        grade=too_many_entries.grade,
        entry_count=len(too_many_entries.entries),
        has_complete_condition=True,
        rating_rule_coverage=too_many_entries.rating_rule_coverage,
    )
    assert too_many_entries.readiness_issues == ("Entry는 최대 18명까지 허용됩니다. (현재 19명)",)
    assert too_many_entries.markets == ()
    assert too_many_entry_choice.is_ready is False


def test_one_entry_match_is_incomplete_and_zero_write() -> None:
    target = _target(entry_count=1)
    repository = RecordingRepository(target)
    commands, factory = _commands(repository)

    with pytest.raises(MatchBettingOpenIncompleteError, match="최소 2명"):
        commands.open_betting(_command(target))

    assert repository.calls == ["lock_target", "find_operation"]
    assert repository.intent is None
    assert repository.audit is None
    assert factory.created[0].commits == 0
    assert factory.created[0].rollbacks == 1


@pytest.mark.parametrize(
    ("target", "expected_issue"),
    (
        (
            _target(current_rule_version_available=False, current_rule_version_complete=False),
            "현재 Rating rule version이 없습니다.",
        ),
        (
            _target(current_rule_version_complete=False),
            "현재 Rating rule version이 완전하지 않습니다.",
        ),
        (
            _target(covered_converted_ranks=(1, 2)),
            "현재 Rating rule version에 G1 · Entry 3명 규칙이 완전하지 않습니다.",
        ),
        (
            _target(
                grade=MatchGrade.LISTED,
                current_rule_version_available=False,
                current_rule_version_complete=False,
            ),
            "현재 Rating rule version이 없습니다.",
        ),
    ),
)
def test_non_op_rating_rule_readiness_is_required(
    target: MatchBettingOpenTarget,
    expected_issue: str,
) -> None:
    assert target.readiness_issues == (expected_issue,)
    choice = MatchBettingOpenTargetChoice(
        match_id=target.match_id,
        match_name=target.match_name,
        grade=target.grade,
        entry_count=len(target.entries),
        has_complete_condition=True,
        rating_rule_coverage=target.rating_rule_coverage,
    )
    assert choice.is_ready is False


def test_listed_requires_only_complete_version_and_op_requires_no_rules() -> None:
    listed = _target(grade=MatchGrade.LISTED)
    op = _target(
        grade=MatchGrade.OP,
        current_rule_version_available=False,
        current_rule_version_complete=False,
    )

    assert listed.rating_rule_coverage.covered_converted_ranks == ()
    assert listed.readiness_issues == ()
    assert op.readiness_issues == ()


def test_incomplete_rating_rule_coverage_is_zero_write() -> None:
    target = _target(covered_converted_ranks=(1, 2))
    repository = RecordingRepository(target)
    commands, factory = _commands(repository)

    with pytest.raises(MatchBettingOpenIncompleteError, match="Entry 3명 규칙"):
        commands.open_betting(_command(target))

    assert repository.calls == ["lock_target", "find_operation"]
    assert repository.intent is None
    assert repository.audit is None
    assert factory.created[0].commits == 0
    assert factory.created[0].rollbacks == 1


@pytest.mark.parametrize(
    ("enabled", "channel_id", "expected_status", "expected_channel"),
    (
        (False, "777777777", PublicationStatus.SUPPRESSED, None),
        (True, None, PublicationStatus.AWAITING_CHANNEL, None),
        (True, "777777777", PublicationStatus.READY, "777777777"),
    ),
)
def test_final_setting_selects_initial_publication_state(
    enabled: bool,
    channel_id: str | None,
    expected_status: PublicationStatus,
    expected_channel: str | None,
) -> None:
    target = _target(enabled=enabled, channel_id=channel_id)
    repository = RecordingRepository(target)
    commands, _ = _commands(repository)

    result = commands.open_betting(_command(target))

    assert result.publication.status == expected_status
    assert result.publication.target_channel_id == expected_channel


def test_stale_incomplete_and_pre_open_bet_reject_without_write() -> None:
    target = _target()
    stale_repository = RecordingRepository(target)
    stale_commands, stale_factory = _commands(stale_repository)
    stale = _command(target)
    stale = OpenMatchBetting(
        match_id=stale.match_id,
        expected_state_fingerprint="0" * 64,
        idempotency_key=stale.idempotency_key,
        actor_discord_user_id=stale.actor_discord_user_id,
        guild_id=stale.guild_id,
    )

    with pytest.raises(MatchBettingOpenStaleError):
        stale_commands.open_betting(stale)
    assert "transition_to_betting_open" not in stale_repository.calls
    assert stale_factory.created[0].rollbacks == 1

    incomplete = _target(condition=None)
    incomplete_repository = RecordingRepository(incomplete)
    incomplete_commands, _ = _commands(incomplete_repository)
    with pytest.raises(MatchBettingOpenIncompleteError):
        incomplete_commands.open_betting(_command(incomplete))
    assert "has_any_bet_facts" not in incomplete_repository.calls

    bet_repository = RecordingRepository(target, has_bet=True)
    bet_commands, _ = _commands(bet_repository)
    with pytest.raises(MatchBettingOpenInvalidSourceError, match="pre-open Bet"):
        bet_commands.open_betting(_command(target))
    assert "transition_to_betting_open" not in bet_repository.calls


def test_exact_retry_uses_stored_after_evidence_without_second_write() -> None:
    target = _target()
    command = _command(target)
    initial_repository = RecordingRepository(target)
    initial_commands, _ = _commands(initial_repository)
    committed = initial_commands.open_betting(command)
    stored = StoredMatchBettingOpenOperation(
        request_fingerprint=command.request_fingerprint,
        type=MatchBettingOpenAuditType.OPENED.value,
        match_id=target.match_id,
        after_data=committed.to_audit_payload(),
    )
    retry_repository = RecordingRepository(_target(status=MatchStatus.BETTING_OPEN), stored=stored)
    retry_commands, retry_factory = _commands(retry_repository)

    retried = retry_commands.open_betting(command)

    assert retried == committed
    assert retry_repository.calls == ["lock_target", "find_operation"]
    assert retry_factory.created[0].commits == 1
