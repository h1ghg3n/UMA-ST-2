"""Native Match Entry replacement application-boundary tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner
from uma_st2.application.match import (
    MatchEntryAccountTarget,
    MatchEntryAuditType,
    MatchEntryCharacterTarget,
    MatchEntryCommands,
    MatchEntryIdempotencyConflictError,
    MatchEntryInvalidSourceError,
    MatchEntryNoChangeError,
    MatchEntryReference,
    MatchEntryRosterSnapshot,
    MatchEntrySnapshot,
    MatchEntryStaleError,
    MatchEntryUnavailableError,
    ReplaceMatchEntries,
    StoredMatchEntryOperation,
)
from uma_st2.domain.identity import GameRegion
from uma_st2.domain.match import MATCH_ENTRY_MAXIMUM_COUNT, MatchSourceKind, MatchStatus

NOW = datetime(2026, 8, 28, 4, 0, tzinfo=UTC)
OLD = datetime(2026, 8, 27, 4, 0, tzinfo=UTC)


def _account(account_id: int = 11, *, region: GameRegion = GameRegion.KR) -> MatchEntryAccountTarget:
    return MatchEntryAccountTarget(
        id=account_id,
        persona_id=f"00000000-0000-0000-0000-{account_id:012d}",
        game_region=region,
        nickname=f"계정 {account_id}",
        affiliation="A조",
    )


def _character(character_id: int = 31, *, variant_id: int | None = None) -> MatchEntryCharacterTarget:
    return MatchEntryCharacterTarget(
        umamusume_id=character_id,
        umamusume_variant_id=variant_id,
        display_name=f"캐릭터 {character_id}",
    )


def _entry(
    *,
    entry_id: int = 51,
    entry_number: int = 1,
    account: MatchEntryAccountTarget | None = None,
    character: MatchEntryCharacterTarget | None = None,
) -> MatchEntrySnapshot:
    account = account or _account()
    character = character or _character()
    return MatchEntrySnapshot(
        entry_id=entry_id,
        entry_number=entry_number,
        game_account_id=account.id,
        owner_at_event_persona_id=account.persona_id,
        affiliation_at_event=account.affiliation,
        game_region=account.game_region,
        game_account_name=account.nickname,
        umamusume_id=character.umamusume_id,
        umamusume_variant_id=character.umamusume_variant_id,
        umamusume_name=character.display_name,
        created_at=OLD,
    )


def _roster(
    *,
    entries: tuple[MatchEntrySnapshot, ...] = (),
    source_kind: MatchSourceKind = MatchSourceKind.NATIVE_V2,
    status: MatchStatus = MatchStatus.SCHEDULED,
) -> MatchEntryRosterSnapshot:
    return MatchEntryRosterSnapshot(
        match_id=71,
        match_name="제12회 정기전",
        source_kind=source_kind,
        status=status,
        entries=entries,
    )


def _command(
    *,
    entries: tuple[MatchEntryReference, ...] | None = None,
    expected: str | None = None,
    key: str = "match-entry-replacement:555",
) -> ReplaceMatchEntries:
    return ReplaceMatchEntries(
        match_id=71,
        entries=entries or (MatchEntryReference(entry_number=1, game_account_id=11, umamusume_id=31),),
        expected_roster_fingerprint=expected or _roster().roster_fingerprint,
        idempotency_key=key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id="555",
        reason="공식 Entry 등록",
    )


class RecordingRepository:
    def __init__(
        self,
        *,
        match: MatchEntryRosterSnapshot | None = None,
        current: tuple[MatchEntrySnapshot, ...] = (),
        stored: StoredMatchEntryOperation | None = None,
        dependents: bool = False,
        accounts: tuple[MatchEntryAccountTarget, ...] | None = None,
        characters: tuple[MatchEntryCharacterTarget, ...] | None = None,
    ) -> None:
        self.match = match if match is not None else _roster()
        self.current = current
        self.stored = stored
        self.dependents = dependents
        self.accounts = accounts or (_account(),)
        self.characters = characters or (_character(),)
        self.calls: list[str] = []
        self.audits: list[tuple[MatchEntryRosterSnapshot, MatchEntryRosterSnapshot]] = []

    def lock_match(self, *, match_id: int) -> MatchEntryRosterSnapshot | None:
        self.calls.append("lock_match")
        assert match_id == 71
        return self.match

    def find_operation(self, *, idempotency_key: str) -> StoredMatchEntryOperation | None:
        self.calls.append("find_operation")
        assert idempotency_key
        return self.stored

    def lock_current_entries(self, *, match_id: int) -> tuple[MatchEntrySnapshot, ...]:
        self.calls.append("lock_current_entries")
        return self.current

    def has_roster_dependents(self, *, match_id: int, entry_ids: tuple[int, ...]) -> bool:
        self.calls.append("has_roster_dependents")
        return self.dependents

    def lock_game_accounts(self, *, account_ids: tuple[int, ...]) -> tuple[MatchEntryAccountTarget, ...]:
        self.calls.append("lock_game_accounts")
        return self.accounts

    def lock_characters(
        self,
        *,
        identities: tuple[tuple[int, int | None], ...],
    ) -> tuple[MatchEntryCharacterTarget, ...]:
        self.calls.append("lock_characters")
        return self.characters

    def replace_entries(
        self,
        *,
        match_id: int,
        entries: tuple[MatchEntryReference, ...],
        accounts: dict[int, MatchEntryAccountTarget],
        characters: dict[tuple[int, int | None], MatchEntryCharacterTarget],
        changed_at: datetime,
    ) -> tuple[MatchEntrySnapshot, ...]:
        self.calls.append("replace_entries")
        return tuple(
            _entry(
                entry_id=100 + entry.entry_number,
                entry_number=entry.entry_number,
                account=accounts[entry.game_account_id],
                character=characters[(entry.umamusume_id, entry.umamusume_variant_id)],
            )
            for entry in entries
        )

    def add_audit(
        self,
        *,
        command: ReplaceMatchEntries,
        before: MatchEntryRosterSnapshot,
        after: MatchEntryRosterSnapshot,
        created_at: datetime,
    ) -> None:
        self.calls.append("add_audit")
        assert command.match_id == 71
        assert created_at == NOW
        self.audits.append((before, after))


@dataclass
class RecordingUnitOfWork:
    match_entries: RecordingRepository
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


def _commands(repository: RecordingRepository) -> tuple[MatchEntryCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return MatchEntryCommands(CommandRunner(factory), clock=lambda: NOW), factory


def test_initial_roster_commit_is_complete_and_pid_free() -> None:
    repository = RecordingRepository()
    commands, factory = _commands(repository)

    result = commands.replace_entries(_command())

    assert result.operation_type == MatchEntryAuditType.REPLACED
    assert len(result.snapshot.entries) == 1
    assert repository.calls == [
        "lock_match",
        "find_operation",
        "lock_current_entries",
        "has_roster_dependents",
        "lock_game_accounts",
        "lock_characters",
        "replace_entries",
        "add_audit",
    ]
    assert "uma_pid" not in str(repository.audits[0][1].to_audit_payload())
    assert factory.created[0].commits == 1


def test_replacement_command_accepts_eighteen_and_rejects_nineteen_entries() -> None:
    references = tuple(
        MatchEntryReference(entry_number=index, game_account_id=index, umamusume_id=100 + index)
        for index in range(1, MATCH_ENTRY_MAXIMUM_COUNT + 1)
    )

    assert len(_command(entries=references).entries) == 18
    with pytest.raises(ValueError, match="at most 18"):
        _command(
            entries=(
                *references,
                MatchEntryReference(entry_number=19, game_account_id=19, umamusume_id=119),
            )
        )


def test_stale_preview_rejects_before_account_or_character_lock() -> None:
    repository = RecordingRepository()
    commands, factory = _commands(repository)

    with pytest.raises(MatchEntryStaleError):
        commands.replace_entries(_command(expected="0" * 64))

    assert repository.calls == ["lock_match", "find_operation", "lock_current_entries"]
    assert factory.created[0].rollbacks == 1


def test_roster_dependents_fail_closed_before_replacement() -> None:
    current = (_entry(),)
    repository = RecordingRepository(current=current, dependents=True)
    commands, factory = _commands(repository)

    with pytest.raises(MatchEntryInvalidSourceError):
        commands.replace_entries(_command(expected=_roster(entries=current).roster_fingerprint))

    assert "replace_entries" not in repository.calls
    assert factory.created[0].rollbacks == 1


@pytest.mark.parametrize(
    ("source_kind", "status"),
    (
        (MatchSourceKind.IMPORTED_V1, MatchStatus.SCHEDULED),
        (MatchSourceKind.NATIVE_V2, MatchStatus.BETTING_OPEN),
    ),
)
def test_imported_or_open_match_rejects_entry_replacement(
    source_kind: MatchSourceKind,
    status: MatchStatus,
) -> None:
    repository = RecordingRepository(match=_roster(source_kind=source_kind, status=status))
    commands, factory = _commands(repository)

    with pytest.raises(MatchEntryUnavailableError):
        commands.replace_entries(_command())

    assert repository.calls == ["lock_match", "find_operation"]
    assert factory.created[0].rollbacks == 1


def test_identical_complete_roster_is_rejected_without_write() -> None:
    current = (_entry(),)
    repository = RecordingRepository(current=current)
    commands, factory = _commands(repository)

    with pytest.raises(MatchEntryNoChangeError):
        commands.replace_entries(_command(expected=_roster(entries=current).roster_fingerprint))

    assert "replace_entries" not in repository.calls
    assert factory.created[0].rollbacks == 1


def test_exact_retry_returns_stored_after_snapshot_without_second_write() -> None:
    command = _command()
    committed = _roster(entries=(_entry(),))
    repository = RecordingRepository(
        stored=StoredMatchEntryOperation(
            request_fingerprint=command.request_fingerprint,
            type=MatchEntryAuditType.REPLACED.value,
            match_id=71,
            after_data=committed.to_audit_payload(),
        )
    )
    commands, factory = _commands(repository)

    result = commands.replace_entries(command)

    assert result.snapshot == committed
    assert repository.calls == ["lock_match", "find_operation"]
    assert factory.created[0].commits == 1


def test_same_key_with_changed_roster_conflicts() -> None:
    original = _command()
    committed = _roster(entries=(_entry(),))
    repository = RecordingRepository(
        stored=StoredMatchEntryOperation(
            request_fingerprint=original.request_fingerprint,
            type=MatchEntryAuditType.REPLACED.value,
            match_id=71,
            after_data=committed.to_audit_payload(),
        )
    )
    commands, factory = _commands(repository)

    with pytest.raises(MatchEntryIdempotencyConflictError):
        commands.replace_entries(_command(entries=(MatchEntryReference(1, 11, 32),)))

    assert factory.created[0].rollbacks == 1
