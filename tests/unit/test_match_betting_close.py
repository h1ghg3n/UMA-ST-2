"""Native Match betting-close Application tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner
from uma_st2.application.match import (
    ClosedMatchBetting,
    CloseMatchBetting,
    MatchBettingCloseAuditType,
    MatchBettingCloseCommands,
    MatchBettingCloseEntry,
    MatchBettingCloseIdempotencyConflictError,
    MatchBettingCloseInvalidSourceError,
    MatchBettingCloseTarget,
    MatchBettingCloseUnavailableError,
    StoredMatchBettingCloseOperation,
    StoredMatchBettingClosePublication,
)
from uma_st2.application.publication import PublicationIntent
from uma_st2.application.publication.match import MatchPublicationDestination
from uma_st2.domain.betting import BetPoolStake, BetType
from uma_st2.domain.match import MatchGrade, MatchSourceKind, MatchStatus
from uma_st2.domain.publication import PublicationStatus

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _target(
    *,
    status: MatchStatus = MatchStatus.BETTING_OPEN,
    source_kind: MatchSourceKind = MatchSourceKind.NATIVE_V2,
    active_bet_count: int = 2,
    active_stake_total: int = 40,
) -> MatchBettingCloseTarget:
    active_bets = (
        (
            BetPoolStake(BetType.WIN, (801,), 10),
            BetPoolStake(BetType.QUINELLA, (801, 802), 30),
        )
        if active_bet_count
        else ()
    )
    return MatchBettingCloseTarget(
        match_id=71,
        match_name="제12회 @everyone 정기전",
        source_kind=source_kind,
        status=status,
        grade=MatchGrade.G1,
        scheduled_at=SCHEDULED_AT,
        entry_count=3,
        active_bet_count=active_bet_count,
        active_stake_total=active_stake_total,
        entries=tuple(MatchBettingCloseEntry(800 + index, index) for index in range(1, 4)),
        active_bets=active_bets,
        destination=MatchPublicationDestination("987654321", True, "777777777"),
    )


def _command(*, key: str = "match-betting-close:555", match_id: int = 71) -> CloseMatchBetting:
    return CloseMatchBetting(
        match_id=match_id,
        idempotency_key=key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id="555",
    )


class RecordingRepository:
    def __init__(
        self,
        target: MatchBettingCloseTarget,
        *,
        stored: StoredMatchBettingCloseOperation | None = None,
        lock_error: ValueError | None = None,
    ) -> None:
        self.target = target
        self.stored = stored
        self.lock_error = lock_error
        self.calls: list[str] = []
        self.audit: tuple[MatchBettingCloseTarget, ClosedMatchBetting] | None = None
        self.intent: PublicationIntent | None = None

    def lock_target(self, *, match_id: int, guild_id: str) -> MatchBettingCloseTarget | None:
        self.calls.append("lock_target")
        assert guild_id == "987654321"
        if self.lock_error is not None:
            raise self.lock_error
        return self.target

    def find_operation(self, *, idempotency_key: str) -> StoredMatchBettingCloseOperation | None:
        self.calls.append("find_operation")
        return self.stored

    def transition_to_betting_closed(self, *, match_id: int, changed_at: datetime) -> None:
        self.calls.append("transition_to_betting_closed")

    def add_publication(
        self,
        *,
        intent: PublicationIntent,
        created_at: datetime,
    ) -> StoredMatchBettingClosePublication:
        self.calls.append("add_publication")
        assert created_at == NOW
        self.intent = intent
        return StoredMatchBettingClosePublication(
            publication_id=91,
            event_key=intent.event_key,
            payload_fingerprint=intent.payload_fingerprint,
            status=PublicationStatus.READY,
            target_channel_id="777777777",
        )

    def add_audit(
        self,
        *,
        command: CloseMatchBetting,
        before: MatchBettingCloseTarget,
        after: ClosedMatchBetting,
        created_at: datetime,
    ) -> None:
        self.calls.append("add_audit")
        assert command.match_id == 71
        assert created_at == NOW
        self.audit = (before, after)


@dataclass
class RecordingUnitOfWork:
    match_betting_close: RecordingRepository
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


def _commands(repository: RecordingRepository) -> tuple[MatchBettingCloseCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return MatchBettingCloseCommands(CommandRunner(factory), clock=lambda: NOW), factory


def test_close_transition_uses_final_locked_aggregate_and_one_audit() -> None:
    repository = RecordingRepository(_target())
    commands, factory = _commands(repository)

    result = commands.close_betting(_command())

    assert result == ClosedMatchBetting(
        match_id=71,
        match_name="제12회 @everyone 정기전",
        status=MatchStatus.BETTING_CLOSED,
        closed_at=NOW,
        entry_count=3,
        active_bet_count=2,
        active_stake_total=40,
        publication=result.publication,
    )
    assert repository.calls == [
        "lock_target",
        "find_operation",
        "transition_to_betting_closed",
        "add_publication",
        "add_audit",
    ]
    assert repository.audit == (repository.target, result)
    assert repository.intent is not None
    assert repository.intent.event_type == "room_match_betting_closed"
    assert repository.intent.event_key == "match:71:betting-close:v1"
    payload = repository.intent.payload_json
    assert payload["publication_type"] == "match_betting_closed"
    assert payload["odds"]["field_multiplier"] == "0.5"
    assert [market["bet_type"] for market in payload["odds"]["markets"]] == ["win", "quinella", "trio"]
    assert all(
        "active_bet_count" not in selection and "stake" not in selection
        for market in payload["odds"]["markets"]
        for selection in market["selections"]
    )
    assert factory.created[0].commits == 1


def test_zero_bet_close_is_allowed() -> None:
    repository = RecordingRepository(_target(active_bet_count=0, active_stake_total=0))
    commands, _ = _commands(repository)

    result = commands.close_betting(_command())

    assert result.active_bet_count == 0
    assert result.active_stake_total == 0
    assert repository.intent is not None
    selection_count = sum(len(market["selections"]) for market in repository.intent.payload_json["odds"]["markets"])
    assert selection_count == 7
    assert "transition_to_betting_closed" in repository.calls


@pytest.mark.parametrize(
    "target",
    (
        _target(status=MatchStatus.BETTING_CLOSED),
        _target(source_kind=MatchSourceKind.IMPORTED_V1),
    ),
)
def test_non_open_or_imported_target_is_zero_write(target: MatchBettingCloseTarget) -> None:
    repository = RecordingRepository(target)
    commands, factory = _commands(repository)

    with pytest.raises(MatchBettingCloseUnavailableError):
        commands.close_betting(_command())

    assert repository.calls == ["lock_target", "find_operation"]
    assert factory.created[0].rollbacks == 1


def test_malformed_locked_source_is_zero_write() -> None:
    repository = RecordingRepository(_target(), lock_error=ValueError("bad marker"))
    commands, factory = _commands(repository)

    with pytest.raises(MatchBettingCloseInvalidSourceError):
        commands.close_betting(_command())

    assert repository.calls == ["lock_target"]
    assert factory.created[0].rollbacks == 1


def test_exact_retry_uses_stored_receipt_and_changed_payload_conflicts() -> None:
    command = _command()
    initial_repository = RecordingRepository(_target())
    initial_commands, _ = _commands(initial_repository)
    committed = initial_commands.close_betting(command)
    stored = StoredMatchBettingCloseOperation(
        request_fingerprint=command.request_fingerprint,
        type=MatchBettingCloseAuditType.CLOSED.value,
        match_id=command.match_id,
        after_data=committed.to_audit_payload(),
    )
    retry_repository = RecordingRepository(_target(status=MatchStatus.BETTING_CLOSED), stored=stored)
    retry_commands, retry_factory = _commands(retry_repository)

    assert retry_commands.close_betting(command) == committed
    assert retry_repository.calls == ["lock_target", "find_operation"]
    assert retry_factory.created[0].commits == 1

    conflict_repository = RecordingRepository(_target(status=MatchStatus.BETTING_CLOSED), stored=stored)
    conflict_commands, _ = _commands(conflict_repository)
    with pytest.raises(MatchBettingCloseIdempotencyConflictError):
        conflict_commands.close_betting(_command(match_id=72))
