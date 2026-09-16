"""Guild-wide periodic Match odds publication Application tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.match import (
    MATCH_ODDS_MODE_OPERATION_TYPE,
    ChangedMatchOddsRefreshMode,
    ChangeMatchOddsRefreshMode,
    MatchOddsPublicationCommands,
    MatchOddsPublicationIdempotencyConflictError,
    MatchOddsPublicationInvalidSourceError,
    MatchOddsPublicationNoChangeError,
    MatchOddsPublicationQueries,
    MatchOddsPublicationUnavailableError,
    MatchOddsRefreshCursor,
    MatchOddsRefreshEntry,
    MatchOddsRefreshStatus,
    MatchOddsRefreshTarget,
    MatchOddsRefreshTargetMatch,
    StoredMatchOddsModeOperation,
    StoredMatchOddsRefreshPublication,
)
from uma_st2.application.publication import (
    MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    MATCH_ODDS_REFRESH_EVENT_TYPE,
    MatchOddsRefreshMode,
    MatchPublicationDestination,
    PublicationIntent,
)
from uma_st2.domain.betting import BetPoolStake, BetType
from uma_st2.domain.match import MatchGrade, MatchSourceKind, MatchStatus

NOW = datetime(2026, 9, 1, 5, 0, tzinfo=UTC)
GUILD_ID = "987654321"


def _cursor(
    *,
    mode: MatchOddsRefreshMode = MatchOddsRefreshMode.NORMAL,
    next_refresh_at: datetime | None = NOW,
    sequence: int = 4,
    fingerprint: str | None = None,
) -> MatchOddsRefreshCursor:
    return MatchOddsRefreshCursor(
        guild_id=GUILD_ID,
        mode=mode,
        next_refresh_at=next_refresh_at,
        sequence=sequence,
        last_projection_fingerprint=fingerprint,
        destination=MatchPublicationDestination(
            guild_id=GUILD_ID,
            announcements_enabled=True,
            target_channel_id="777777777",
        ),
    )


def _match(
    *,
    match_id: int = 71,
    name: str = "제12회 정기전",
    scheduled_at: datetime = datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
    bet_entry_id: int = 101,
) -> MatchOddsRefreshTargetMatch:
    entries = tuple(MatchOddsRefreshEntry(100 + index, index) for index in range(1, 4))
    return MatchOddsRefreshTargetMatch(
        match_id=match_id,
        match_name=name,
        source_kind=MatchSourceKind.NATIVE_V2,
        status=MatchStatus.BETTING_OPEN,
        grade=MatchGrade.G1,
        scheduled_at=scheduled_at,
        entries=entries,
        active_bets=(BetPoolStake(BetType.WIN, (bet_entry_id,), 10),),
    )


def _target(
    *,
    cursor: MatchOddsRefreshCursor | None = None,
    matches: tuple[MatchOddsRefreshTargetMatch, ...] | None = None,
) -> MatchOddsRefreshTarget:
    return MatchOddsRefreshTarget(
        cursor=cursor or _cursor(),
        matches=matches if matches is not None else (_match(),),
    )


class RecordingRepository:
    def __init__(
        self,
        target: MatchOddsRefreshTarget | None,
        *,
        stored: StoredMatchOddsModeOperation | None = None,
        lock_error: ValueError | None = None,
    ) -> None:
        self.target = target
        self.stored = stored
        self.lock_error = lock_error
        self.calls: list[str] = []
        self.updated: list[MatchOddsRefreshCursor] = []
        self.intents: list[PublicationIntent] = []
        self.audits: list[tuple[ChangeMatchOddsRefreshMode, MatchOddsRefreshCursor, ChangedMatchOddsRefreshMode]] = []

    def lock_target(self, *, guild_id: str) -> MatchOddsRefreshTarget | None:
        self.calls.append("lock_target")
        assert guild_id == GUILD_ID
        if self.lock_error is not None:
            raise self.lock_error
        return self.target

    def find_operation(self, *, idempotency_key: str) -> StoredMatchOddsModeOperation | None:
        self.calls.append("find_operation")
        return self.stored

    def update_cursor(self, *, cursor: MatchOddsRefreshCursor, changed_at: datetime) -> None:
        self.calls.append("update_cursor")
        assert changed_at >= NOW
        self.updated.append(cursor)

    def add_publication(
        self,
        *,
        intent: PublicationIntent,
        created_at: datetime,
    ) -> StoredMatchOddsRefreshPublication:
        self.calls.append("add_publication")
        assert created_at >= NOW
        self.intents.append(intent)
        return StoredMatchOddsRefreshPublication(
            publication_id=900 + len(self.intents),
            event_key=intent.event_key,
            payload_fingerprint=intent.payload_fingerprint,
            status=intent.status,
            target_channel_id=intent.target_channel_id,
        )

    def add_mode_audit(
        self,
        *,
        command: ChangeMatchOddsRefreshMode,
        before: MatchOddsRefreshCursor,
        after: ChangedMatchOddsRefreshMode,
        created_at: datetime,
    ) -> None:
        self.calls.append("add_mode_audit")
        assert created_at >= NOW
        self.audits.append((command, before, after))


@dataclass
class RecordingUnitOfWork:
    match_odds_publication: RecordingRepository
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


def _commands(
    repository: RecordingRepository,
    *,
    now: datetime = NOW,
) -> tuple[MatchOddsPublicationCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return MatchOddsPublicationCommands(CommandRunner(factory), clock=lambda: now), factory


def _mode_command(
    mode: MatchOddsRefreshMode,
    *,
    key: str = "match-odds-mode:555",
) -> ChangeMatchOddsRefreshMode:
    return ChangeMatchOddsRefreshMode(
        guild_id=GUILD_ID,
        desired_mode=mode,
        actor_discord_user_id="123456789",
        idempotency_key=key,
        correlation_id="555",
    )


def test_missing_cycle_starts_normal_ten_minutes_after_worker_observation() -> None:
    repository = RecordingRepository(_target(cursor=_cursor(mode=MatchOddsRefreshMode.LIVE, next_refresh_at=None)))
    commands, factory = _commands(repository)

    result = commands.refresh_due(guild_id=GUILD_ID)

    assert result.mode is MatchOddsRefreshMode.NORMAL
    assert result.next_refresh_at == NOW + timedelta(minutes=10)
    assert result.publication is None
    assert result.cursor_changed is True
    assert repository.calls == ["lock_target", "update_cursor"]
    assert repository.updated[0].sequence == 4
    assert repository.updated[0].last_projection_fingerprint is None
    assert factory.created[0].commits == 1


def test_not_due_is_a_zero_write_check() -> None:
    due = NOW + timedelta(minutes=3)
    repository = RecordingRepository(_target(cursor=_cursor(next_refresh_at=due)))
    commands, factory = _commands(repository)

    result = commands.refresh_due(guild_id=GUILD_ID)

    assert result.next_refresh_at == due
    assert result.cursor_changed is False
    assert result.publication is None
    assert repository.calls == ["lock_target"]
    assert factory.created[0].commits == 1


def test_due_refresh_publishes_all_matches_in_stable_order_and_advances_cursor() -> None:
    later = _match(match_id=72, name="두 번째 경기", scheduled_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC))
    earlier = _match()
    repository = RecordingRepository(_target(matches=(later, earlier)))
    commands, factory = _commands(repository)

    result = commands.refresh_due(guild_id=GUILD_ID)

    assert result.open_match_count == 2
    assert result.next_refresh_at == NOW + timedelta(minutes=10)
    assert result.publication is not None
    assert repository.calls == ["lock_target", "add_publication", "update_cursor"]
    intent = repository.intents[0]
    assert intent.destination_kind == MATCH_ANNOUNCEMENT_DESTINATION_KIND
    assert intent.event_type == MATCH_ODDS_REFRESH_EVENT_TYPE
    assert intent.event_key == "match-odds-refresh:5:v1"
    assert [match["name"] for match in intent.payload_json["matches"]] == ["제12회 정기전", "두 번째 경기"]  # type: ignore[index]
    assert intent.payload_json["coverage"]["cadence_seconds"] == 600  # type: ignore[index]
    assert repository.updated[0].sequence == 5
    assert repository.updated[0].last_projection_fingerprint is not None
    assert all(
        protected not in str(intent.payload_json)
        for protected in ("participant_count", "pool_amount", "persona_id", "bet_id", "stake")
    )
    assert factory.created[0].commits == 1


def test_unchanged_due_projection_advances_time_without_delivery_or_sequence() -> None:
    initial_repository = RecordingRepository(_target())
    initial_commands, _ = _commands(initial_repository)
    initial_commands.refresh_due(guild_id=GUILD_ID)
    committed_cursor = initial_repository.updated[0]
    second_now = NOW + timedelta(minutes=10)
    repository = RecordingRepository(
        _target(
            cursor=MatchOddsRefreshCursor(
                guild_id=committed_cursor.guild_id,
                mode=committed_cursor.mode,
                next_refresh_at=second_now,
                sequence=committed_cursor.sequence,
                last_projection_fingerprint=committed_cursor.last_projection_fingerprint,
                destination=committed_cursor.destination,
            )
        )
    )
    commands, _ = _commands(repository, now=second_now)

    result = commands.refresh_due(guild_id=GUILD_ID)

    assert result.publication is None
    assert repository.calls == ["lock_target", "update_cursor"]
    assert repository.updated[0].sequence == committed_cursor.sequence
    assert repository.updated[0].next_refresh_at == second_now + timedelta(minutes=10)
    assert repository.updated[0].last_projection_fingerprint == committed_cursor.last_projection_fingerprint


def test_partial_close_preserves_live_cycle_until_remaining_projection_is_due() -> None:
    next_due = NOW + timedelta(seconds=30)
    preserved_cursor = _cursor(
        mode=MatchOddsRefreshMode.LIVE,
        next_refresh_at=next_due,
        fingerprint="a" * 64,
    )
    remaining = _match(match_id=72, name="남은 경기")
    before_due_repository = RecordingRepository(_target(cursor=preserved_cursor, matches=(remaining,)))
    before_due_commands, _ = _commands(before_due_repository)

    before_due = before_due_commands.refresh_due(guild_id=GUILD_ID)

    assert before_due.mode is MatchOddsRefreshMode.LIVE
    assert before_due.open_match_count == 1
    assert before_due.next_refresh_at == next_due
    assert before_due.cursor_changed is False
    assert before_due_repository.calls == ["lock_target"]

    due_repository = RecordingRepository(_target(cursor=preserved_cursor, matches=(remaining,)))
    due_commands, _ = _commands(due_repository, now=next_due)

    due = due_commands.refresh_due(guild_id=GUILD_ID)

    assert due.mode is MatchOddsRefreshMode.LIVE
    assert due.open_match_count == 1
    assert due.next_refresh_at == next_due + timedelta(minutes=1)
    assert due.publication is not None
    assert [match["name"] for match in due_repository.intents[0].payload_json["matches"]] == ["남은 경기"]  # type: ignore[index]


def test_last_open_match_disappearing_resets_normal_cycle_without_publication() -> None:
    repository = RecordingRepository(
        _target(
            cursor=_cursor(
                mode=MatchOddsRefreshMode.LIVE,
                next_refresh_at=NOW + timedelta(minutes=1),
                fingerprint="a" * 64,
            ),
            matches=(),
        )
    )
    commands, _ = _commands(repository)

    result = commands.refresh_due(guild_id=GUILD_ID)

    assert result.mode is MatchOddsRefreshMode.NORMAL
    assert result.next_refresh_at is None
    assert result.open_match_count == 0
    assert repository.updated[0].last_projection_fingerprint is None
    assert repository.updated[0].sequence == 4
    assert repository.intents == []


def test_normal_to_live_publishes_immediately_and_persists_exact_retry_evidence() -> None:
    repository = RecordingRepository(_target(cursor=_cursor(next_refresh_at=NOW + timedelta(minutes=5))))
    commands, _ = _commands(repository)
    command = _mode_command(MatchOddsRefreshMode.LIVE)

    result = commands.change_mode(command)

    assert result.previous_mode is MatchOddsRefreshMode.NORMAL
    assert result.mode is MatchOddsRefreshMode.LIVE
    assert result.next_refresh_at == NOW + timedelta(minutes=1)
    assert result.publication is not None
    assert repository.calls == [
        "lock_target",
        "find_operation",
        "add_publication",
        "update_cursor",
        "add_mode_audit",
    ]
    assert repository.updated[0].sequence == 5
    assert repository.intents[0].payload_json["coverage"]["mode"] == "live"  # type: ignore[index]
    assert repository.audits[0][2] == result

    stored = StoredMatchOddsModeOperation(
        request_fingerprint=command.request_fingerprint,
        type=MATCH_ODDS_MODE_OPERATION_TYPE,
        guild_id=GUILD_ID,
        after_data=result.to_payload(),
    )
    retry_repository = RecordingRepository(
        _target(cursor=repository.updated[0]),
        stored=stored,
    )
    retry_commands, _ = _commands(retry_repository, now=NOW + timedelta(seconds=20))

    assert retry_commands.change_mode(command) == result
    assert retry_repository.calls == ["lock_target", "find_operation"]


def test_live_to_normal_waits_ten_minutes_without_immediate_publication() -> None:
    repository = RecordingRepository(
        _target(
            cursor=_cursor(
                mode=MatchOddsRefreshMode.LIVE,
                next_refresh_at=NOW + timedelta(seconds=20),
                fingerprint="b" * 64,
            )
        )
    )
    commands, _ = _commands(repository)

    result = commands.change_mode(_mode_command(MatchOddsRefreshMode.NORMAL))

    assert result.mode is MatchOddsRefreshMode.NORMAL
    assert result.next_refresh_at == NOW + timedelta(minutes=10)
    assert result.publication is None
    assert repository.intents == []
    assert repository.updated[0].sequence == 4
    assert repository.updated[0].last_projection_fingerprint == "b" * 64


def test_mode_change_rejects_no_open_match_same_mode_and_changed_key_without_write() -> None:
    no_match = RecordingRepository(_target(matches=()))
    no_match_commands, no_match_factory = _commands(no_match)
    with pytest.raises(MatchOddsPublicationUnavailableError):
        no_match_commands.change_mode(_mode_command(MatchOddsRefreshMode.LIVE))
    assert no_match.calls == ["lock_target", "find_operation"]
    assert no_match_factory.created[0].rollbacks == 1

    same = RecordingRepository(_target())
    same_commands, _ = _commands(same)
    with pytest.raises(MatchOddsPublicationNoChangeError):
        same_commands.change_mode(_mode_command(MatchOddsRefreshMode.NORMAL))
    assert same.calls == ["lock_target", "find_operation"]

    command = _mode_command(MatchOddsRefreshMode.LIVE)
    conflict = RecordingRepository(
        _target(),
        stored=StoredMatchOddsModeOperation(
            request_fingerprint="c" * 64,
            type=MATCH_ODDS_MODE_OPERATION_TYPE,
            guild_id=GUILD_ID,
            after_data=None,
        ),
    )
    conflict_commands, _ = _commands(conflict)
    with pytest.raises(MatchOddsPublicationIdempotencyConflictError):
        conflict_commands.change_mode(command)
    assert conflict.calls == ["lock_target", "find_operation"]


def test_malformed_current_pool_fails_closed_without_cursor_or_publication_write() -> None:
    malformed = _match(bet_entry_id=999)
    repository = RecordingRepository(_target(matches=(malformed,)))
    commands, factory = _commands(repository)

    with pytest.raises(MatchOddsPublicationInvalidSourceError):
        commands.refresh_due(guild_id=GUILD_ID)

    assert repository.calls == ["lock_target"]
    assert factory.created[0].rollbacks == 1


@dataclass
class RecordingQueryRepository:
    status: MatchOddsRefreshStatus | None

    def get_status(self, *, guild_id: str) -> MatchOddsRefreshStatus | None:
        assert guild_id == GUILD_ID
        return self.status


@dataclass
class RecordingQueryUnitOfWork:
    match_odds_publication_queries: RecordingQueryRepository
    rollbacks: int = 0

    def __enter__(self) -> RecordingQueryUnitOfWork:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def commit(self) -> None:
        raise AssertionError("Query UoW must not commit.")

    def rollback(self) -> None:
        self.rollbacks += 1


def test_status_query_is_read_only_and_missing_setting_is_unavailable() -> None:
    status = MatchOddsRefreshStatus(GUILD_ID, MatchOddsRefreshMode.LIVE, NOW + timedelta(minutes=1), 2)
    unit_of_work = RecordingQueryUnitOfWork(RecordingQueryRepository(status))
    queries = MatchOddsPublicationQueries(QueryRunner(lambda: unit_of_work))

    assert queries.get_status(guild_id=GUILD_ID) == status
    assert unit_of_work.rollbacks == 1

    missing = MatchOddsPublicationQueries(QueryRunner(lambda: RecordingQueryUnitOfWork(RecordingQueryRepository(None))))
    with pytest.raises(MatchOddsPublicationUnavailableError):
        missing.get_status(guild_id=GUILD_ID)
