"""Staff Match Entry candidate and exact-selection projection tests."""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType

import pytest

from uma_st2.application.execution import QueryRunner
from uma_st2.application.match import (
    MatchEntryAccountTarget,
    MatchEntryCharacterTarget,
    MatchEntryRosterSnapshot,
    MatchEntrySearchLine,
    MatchEntrySelection,
    MatchEntryTargetChoice,
    MatchStaffEntryLookupError,
    MatchStaffEntryQueries,
    MatchStaffEntryUnavailableError,
)
from uma_st2.domain.identity import GameRegion
from uma_st2.domain.match import MATCH_ENTRY_MAXIMUM_COUNT, MatchSourceKind, MatchStatus


def _target(
    *,
    source_kind: MatchSourceKind = MatchSourceKind.NATIVE_V2,
    status: MatchStatus = MatchStatus.SCHEDULED,
) -> MatchEntryRosterSnapshot:
    return MatchEntryRosterSnapshot(
        match_id=71,
        match_name="제12회 정기전",
        source_kind=source_kind,
        status=status,
    )


class RecordingRepository:
    def __init__(self) -> None:
        self.target: MatchEntryRosterSnapshot | None = _target()
        self.accounts = (
            MatchEntryAccountTarget(11, "00000000-0000-0000-0000-000000000011", GameRegion.KR, "계정 11"),
            MatchEntryAccountTarget(12, "00000000-0000-0000-0000-000000000012", GameRegion.KR, "계정 12"),
        )
        self.characters = (
            MatchEntryCharacterTarget(31, None, "스페셜 위크"),
            MatchEntryCharacterTarget(32, 41, "수영복 스페셜 위크"),
        )
        self.search_calls: list[tuple[str, int]] = []
        self.account_candidate_calls: list[tuple[str, int]] = []
        self.character_list_calls = 0

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchEntryTargetChoice, ...]:
        self.search_calls.append((search, limit))
        return (MatchEntryTargetChoice(71, "제12회 정기전", MatchStatus.SCHEDULED, 0),)

    def get_target(self, *, match_id: int) -> MatchEntryRosterSnapshot | None:
        return self.target

    def search_game_accounts(self, *, chunk: str, limit: int) -> tuple[MatchEntryAccountTarget, ...]:
        self.account_candidate_calls.append((chunk, limit))
        return self.accounts

    def list_characters(self) -> tuple[MatchEntryCharacterTarget, ...]:
        self.character_list_calls += 1
        return self.characters

    def resolve_game_accounts(self, *, ids: tuple[int, ...]) -> tuple[MatchEntryAccountTarget, ...]:
        by_id = {item.id: item for item in self.accounts}
        return tuple(by_id[item] for item in ids if item in by_id)

    def resolve_characters(
        self,
        *,
        identities: tuple[tuple[int, int | None], ...],
    ) -> tuple[MatchEntryCharacterTarget, ...]:
        by_id = {item.identity: item for item in self.characters}
        return tuple(by_id[item] for item in identities if item in by_id)


@dataclass
class RecordingUnitOfWork:
    match_staff_entry_queries: RecordingRepository

    def __enter__(self) -> RecordingUnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return False

    def commit(self) -> None:
        raise AssertionError("QueryRunner must not commit")

    def rollback(self) -> None:
        pass


def _queries(repository: RecordingRepository) -> MatchStaffEntryQueries:
    return MatchStaffEntryQueries(QueryRunner(lambda: RecordingUnitOfWork(repository)))


def test_prepare_candidates_returns_bounded_rows_without_automatic_selection() -> None:
    repository = RecordingRepository()
    lines = (
        MatchEntrySearchLine("계정 1"),
        MatchEntrySearchLine("계정 2"),
    )

    draft = _queries(repository).prepare_candidates(match_id=71, lines=lines)

    assert [row.entry_number for row in draft.rows] == [1, 2]
    assert draft.rows[0].accounts == repository.accounts
    assert draft.characters == repository.characters
    assert repository.account_candidate_calls == [("계정 1", 25), ("계정 2", 25)]
    assert repository.character_list_calls == 1


def test_prepare_replacement_resolves_explicit_pid_free_selections() -> None:
    repository = RecordingRepository()

    draft = _queries(repository).prepare_replacement(
        match_id=71,
        selections=(
            MatchEntrySelection(11, 31),
            MatchEntrySelection(12, 32, 41),
        ),
    )

    assert [entry.entry_number for entry in draft.desired_entries] == [1, 2]
    assert [entry.reference.game_account_id for entry in draft.desired_entries] == [11, 12]
    assert [entry.character.identity for entry in draft.desired_entries] == [(31, None), (32, 41)]


def test_prepare_replacement_preserves_multiple_characters_for_one_game_account() -> None:
    repository = RecordingRepository()

    draft = _queries(repository).prepare_replacement(
        match_id=71,
        selections=(
            MatchEntrySelection(11, 31),
            MatchEntrySelection(11, 32, 41),
        ),
    )

    assert [entry.reference.game_account_id for entry in draft.desired_entries] == [11, 11]


def test_missing_candidate_or_stale_selected_row_fails_without_partial_draft() -> None:
    repository = RecordingRepository()
    repository.accounts = ()
    queries = _queries(repository)

    with pytest.raises(MatchStaffEntryLookupError, match="GameAccount chunk"):
        queries.prepare_candidates(match_id=71, lines=(MatchEntrySearchLine("없음"),))
    with pytest.raises(MatchStaffEntryLookupError, match="selected GameAccount"):
        queries.prepare_replacement(match_id=71, selections=(MatchEntrySelection(999, 31),))


def test_missing_canonical_umamusume_fails_before_account_candidate_search() -> None:
    repository = RecordingRepository()
    repository.characters = ()

    with pytest.raises(MatchStaffEntryLookupError, match="canonical Umamusume"):
        _queries(repository).prepare_candidates(match_id=71, lines=(MatchEntrySearchLine("계정"),))

    assert repository.character_list_calls == 1
    assert repository.account_candidate_calls == []


def test_more_than_eighteen_input_lines_reject_before_query_uow() -> None:
    lines = tuple(MatchEntrySearchLine(f"계정 {index}") for index in range(1, MATCH_ENTRY_MAXIMUM_COUNT + 2))

    def should_not_open_uow() -> RecordingUnitOfWork:
        raise AssertionError("over-limit input must reject before opening a query UoW")

    with pytest.raises(ValueError, match="at most 18"):
        MatchStaffEntryQueries(QueryRunner(should_not_open_uow)).prepare_candidates(match_id=71, lines=lines)


@pytest.mark.parametrize(
    ("source_kind", "status"),
    (
        (MatchSourceKind.IMPORTED_V1, MatchStatus.SCHEDULED),
        (MatchSourceKind.NATIVE_V2, MatchStatus.BETTING_OPEN),
    ),
)
def test_non_native_or_open_target_is_not_editable(
    source_kind: MatchSourceKind,
    status: MatchStatus,
) -> None:
    repository = RecordingRepository()
    repository.target = _target(source_kind=source_kind, status=status)

    with pytest.raises(MatchStaffEntryUnavailableError):
        _queries(repository).get_target(match_id=71)


def test_search_is_trimmed_and_bounded() -> None:
    repository = RecordingRepository()

    result = _queries(repository).search_targets(search="  정기전  ", limit=25)

    assert result[0].match_id == 71
    assert repository.search_calls == [("정기전", 25)]
