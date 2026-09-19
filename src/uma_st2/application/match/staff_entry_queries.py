"""Read-only staff projections for native Match Entry replacement."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol

from uma_st2.application.execution import QueryRunner, UnitOfWork
from uma_st2.domain.match import MATCH_ENTRY_MAXIMUM_COUNT, MatchGrade, MatchSourceKind, MatchStatus

from .entries import (
    MatchEntryAccountTarget,
    MatchEntryCharacterTarget,
    MatchEntryReference,
    MatchEntryRosterSnapshot,
)

_EDITABLE_STATUSES: Final = frozenset({MatchStatus.SCHEDULED, MatchStatus.ENTRY_CONFIRMED})


class MatchStaffEntryQueryError(ValueError):
    """Base error for rejected staff Entry projections."""


class MatchStaffEntryUnavailableError(MatchStaffEntryQueryError):
    """The selected Match is no longer an eligible Entry target."""


class MatchStaffEntryLookupError(MatchStaffEntryQueryError):
    """A current GameAccount or canonical Umamusume lookup is unresolved."""


class MatchStaffEntryInvalidSourceError(MatchStaffEntryQueryError):
    """Stored Match/Entry facts are malformed."""


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _normalized_string(value: str, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field_name} must be a non-empty string of at most {max_length} characters.")
    return normalized


@dataclass(frozen=True, slots=True)
class MatchEntrySearchLine:
    """One zero-write GameAccount candidate-search line."""

    account_chunk: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "account_chunk",
            _normalized_string(self.account_chunk, field_name="account_chunk", max_length=100),
        )


@dataclass(frozen=True, slots=True)
class MatchEntryCandidateRow:
    """Bounded current candidates for one requested Entry position."""

    entry_number: int
    search: MatchEntrySearchLine
    accounts: tuple[MatchEntryAccountTarget, ...]

    def __post_init__(self) -> None:
        _require_positive_int(self.entry_number, field_name="entry_number")
        if not isinstance(self.search, MatchEntrySearchLine):
            raise ValueError("search must be MatchEntrySearchLine.")
        object.__setattr__(self, "accounts", tuple(self.accounts))
        if any(not isinstance(item, MatchEntryAccountTarget) for item in self.accounts):
            raise ValueError("accounts must contain MatchEntryAccountTarget values.")
        if len(self.accounts) > 25:
            raise ValueError("accounts must contain at most 25 candidates.")


@dataclass(frozen=True, slots=True)
class MatchEntryCandidateDraft:
    """Detached candidate pages for a complete zero-write roster input."""

    current: MatchEntryRosterSnapshot
    rows: tuple[MatchEntryCandidateRow, ...]
    characters: tuple[MatchEntryCharacterTarget, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.current, MatchEntryRosterSnapshot):
            raise ValueError("current must be MatchEntryRosterSnapshot.")
        ordered = tuple(sorted(self.rows, key=lambda row: row.entry_number))
        if not ordered or tuple(row.entry_number for row in ordered) != tuple(range(1, len(ordered) + 1)):
            raise ValueError("rows must be non-empty and contiguous from 1.")
        if len(ordered) > MATCH_ENTRY_MAXIMUM_COUNT:
            raise ValueError(f"rows must contain at most {MATCH_ENTRY_MAXIMUM_COUNT} Entries.")
        characters = tuple(self.characters)
        if not characters or any(not isinstance(item, MatchEntryCharacterTarget) for item in characters):
            raise ValueError("characters must contain current MatchEntryCharacterTarget values.")
        if len({item.identity for item in characters}) != len(characters):
            raise ValueError("characters must contain unique canonical identities.")
        object.__setattr__(self, "rows", ordered)
        object.__setattr__(self, "characters", characters)


@dataclass(frozen=True, slots=True)
class MatchEntrySelection:
    """One explicit candidate choice used to build a canonical replacement draft."""

    game_account_id: int
    umamusume_id: int
    umamusume_variant_id: int | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.game_account_id, field_name="game_account_id")
        _require_positive_int(self.umamusume_id, field_name="umamusume_id")
        if self.umamusume_variant_id is not None:
            _require_positive_int(self.umamusume_variant_id, field_name="umamusume_variant_id")


@dataclass(frozen=True, slots=True)
class MatchEntryTargetChoice:
    """One bounded native pre-open Match selector row."""

    match_id: int
    match_name: str
    status: MatchStatus
    current_entry_count: int
    grade: MatchGrade
    scheduled_at: datetime

    def __post_init__(self) -> None:
        _require_positive_int(self.match_id, field_name="match_id")
        object.__setattr__(
            self, "match_name", _normalized_string(self.match_name, field_name="match_name", max_length=200)
        )
        object.__setattr__(self, "status", MatchStatus(self.status))
        if (
            isinstance(self.current_entry_count, bool)
            or not isinstance(self.current_entry_count, int)
            or self.current_entry_count < 0
        ):
            raise ValueError("current_entry_count must be a non-negative integer.")


@dataclass(frozen=True, slots=True)
class MatchEntryDraftEntry:
    """One PID-free resolved desired Entry shown to staff."""

    entry_number: int
    account: MatchEntryAccountTarget
    character: MatchEntryCharacterTarget

    def __post_init__(self) -> None:
        _require_positive_int(self.entry_number, field_name="entry_number")
        if not isinstance(self.account, MatchEntryAccountTarget):
            raise ValueError("account must be MatchEntryAccountTarget.")
        if not isinstance(self.character, MatchEntryCharacterTarget):
            raise ValueError("character must be MatchEntryCharacterTarget.")

    @property
    def reference(self) -> MatchEntryReference:
        return MatchEntryReference(
            entry_number=self.entry_number,
            game_account_id=self.account.id,
            umamusume_id=self.character.umamusume_id,
            umamusume_variant_id=self.character.umamusume_variant_id,
        )


@dataclass(frozen=True, slots=True)
class MatchEntryReplacementDraft:
    """Complete closed-session before/after roster draft."""

    current: MatchEntryRosterSnapshot
    desired_entries: tuple[MatchEntryDraftEntry, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.current, MatchEntryRosterSnapshot):
            raise ValueError("current must be MatchEntryRosterSnapshot.")
        ordered = tuple(sorted(self.desired_entries, key=lambda entry: entry.entry_number))
        if not ordered or tuple(entry.entry_number for entry in ordered) != tuple(range(1, len(ordered) + 1)):
            raise ValueError("desired_entries must be non-empty and contiguous from 1.")
        if len(ordered) > MATCH_ENTRY_MAXIMUM_COUNT:
            raise ValueError(f"desired_entries must contain at most {MATCH_ENTRY_MAXIMUM_COUNT} Entries.")
        if len({entry.account.game_region for entry in ordered}) != 1:
            raise ValueError("desired_entries must share one game region.")
        object.__setattr__(self, "desired_entries", ordered)

    @property
    def command_entries(self) -> tuple[MatchEntryReference, ...]:
        return tuple(entry.reference for entry in self.desired_entries)


class MatchStaffEntryQueryRepository(Protocol):
    """Read-only persistence operations for Entry editor lookups."""

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchEntryTargetChoice, ...]: ...

    def get_target(self, *, match_id: int) -> MatchEntryRosterSnapshot | None: ...

    def search_game_accounts(
        self,
        *,
        chunk: str,
        limit: int,
    ) -> tuple[MatchEntryAccountTarget, ...]: ...

    def list_characters(self) -> tuple[MatchEntryCharacterTarget, ...]: ...

    def resolve_game_accounts(self, *, ids: tuple[int, ...]) -> tuple[MatchEntryAccountTarget, ...]: ...

    def resolve_characters(
        self,
        *,
        identities: tuple[tuple[int, int | None], ...],
    ) -> tuple[MatchEntryCharacterTarget, ...]: ...


class MatchStaffEntryQueryUnitOfWork(UnitOfWork, Protocol):
    """Feature query UoW exposing only Match Entry projections."""

    @property
    def match_staff_entry_queries(self) -> MatchStaffEntryQueryRepository: ...


@dataclass(frozen=True, slots=True)
class MatchStaffEntryQueries:
    """Application entry point for bounded candidate search and exact selection."""

    query_runner: QueryRunner[MatchStaffEntryQueryUnitOfWork]

    def search_targets(self, *, search: str, limit: int = 25) -> tuple[MatchEntryTargetChoice, ...]:
        if not isinstance(search, str) or len(search) > 200:
            raise ValueError("search must be a string of at most 200 characters.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be an integer from 1 through 25.")
        return self.query_runner.run(
            lambda unit_of_work: unit_of_work.match_staff_entry_queries.search_targets(
                search=search.strip(),
                limit=limit,
            )
        )

    def get_target(self, *, match_id: int) -> MatchEntryRosterSnapshot:
        _require_positive_int(match_id, field_name="match_id")

        def query(unit_of_work: MatchStaffEntryQueryUnitOfWork) -> MatchEntryRosterSnapshot:
            try:
                target = unit_of_work.match_staff_entry_queries.get_target(match_id=match_id)
            except (TypeError, ValueError) as exc:
                raise MatchStaffEntryInvalidSourceError("Stored Match Entry target is malformed.") from exc
            return self._require_eligible(target)

        return self.query_runner.run(query)

    def prepare_editor(self, *, match_id: int) -> MatchEntryCandidateDraft | MatchEntryRosterSnapshot:
        """Prefill saved choices by exact IDs; an empty roster starts bulk input."""
        _require_positive_int(match_id, field_name="match_id")

        def query(unit_of_work: MatchStaffEntryQueryUnitOfWork) -> MatchEntryCandidateDraft | MatchEntryRosterSnapshot:
            repository = unit_of_work.match_staff_entry_queries
            current = self._require_eligible(repository.get_target(match_id=match_id))
            if not current.entries:
                return current
            accounts = repository.resolve_game_accounts(ids=tuple(entry.game_account_id for entry in current.entries))
            if len(accounts) != len(current.entries):
                raise MatchStaffEntryLookupError("Every saved GameAccount must still resolve exactly once.")
            return MatchEntryCandidateDraft(
                current=current,
                rows=tuple(
                    MatchEntryCandidateRow(
                        entry_number=entry.entry_number,
                        search=MatchEntrySearchLine(account.nickname),
                        accounts=(account,),
                    )
                    for entry, account in zip(current.entries, accounts, strict=True)
                ),
                characters=repository.list_characters(),
            )

        return self.query_runner.run(query)

    def prepare_candidates(
        self,
        *,
        match_id: int,
        lines: tuple[MatchEntrySearchLine, ...],
        limit: int = 25,
    ) -> MatchEntryCandidateDraft:
        _require_positive_int(match_id, field_name="match_id")
        if not lines or any(not isinstance(line, MatchEntrySearchLine) for line in lines):
            raise ValueError("lines must contain at least one MatchEntrySearchLine.")
        if len(lines) > MATCH_ENTRY_MAXIMUM_COUNT:
            raise ValueError(f"lines must contain at most {MATCH_ENTRY_MAXIMUM_COUNT} Match Entries.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be an integer from 1 through 25.")

        def query(unit_of_work: MatchStaffEntryQueryUnitOfWork) -> MatchEntryCandidateDraft:
            repository = unit_of_work.match_staff_entry_queries
            try:
                current = self._require_eligible(repository.get_target(match_id=match_id))
                characters = repository.list_characters()
                if not characters:
                    raise MatchStaffEntryLookupError("No current canonical Umamusume is available.")
                rows: list[MatchEntryCandidateRow] = []
                for entry_number, line in enumerate(lines, start=1):
                    accounts = repository.search_game_accounts(chunk=line.account_chunk, limit=limit)
                    rows.append(
                        MatchEntryCandidateRow(
                            entry_number=entry_number,
                            search=line,
                            accounts=accounts,
                        )
                    )
            except MatchStaffEntryQueryError:
                raise
            except (TypeError, ValueError) as exc:
                raise MatchStaffEntryInvalidSourceError("Stored Match Entry lookup source is malformed.") from exc
            return MatchEntryCandidateDraft(current=current, rows=tuple(rows), characters=characters)

        return self.query_runner.run(query)

    def prepare_replacement(
        self,
        *,
        match_id: int,
        selections: tuple[MatchEntrySelection, ...],
    ) -> MatchEntryReplacementDraft:
        _require_positive_int(match_id, field_name="match_id")
        if not selections or any(not isinstance(selection, MatchEntrySelection) for selection in selections):
            raise ValueError("selections must contain at least one MatchEntrySelection.")
        if len(selections) > MATCH_ENTRY_MAXIMUM_COUNT:
            raise ValueError(f"selections must contain at most {MATCH_ENTRY_MAXIMUM_COUNT} Match Entries.")

        def query(unit_of_work: MatchStaffEntryQueryUnitOfWork) -> MatchEntryReplacementDraft:
            repository = unit_of_work.match_staff_entry_queries
            try:
                current = self._require_eligible(repository.get_target(match_id=match_id))
                accounts = repository.resolve_game_accounts(ids=tuple(item.game_account_id for item in selections))
                characters = repository.resolve_characters(
                    identities=tuple((item.umamusume_id, item.umamusume_variant_id) for item in selections)
                )
            except MatchStaffEntryQueryError:
                raise
            except (TypeError, ValueError) as exc:
                raise MatchStaffEntryInvalidSourceError("Stored Match Entry lookup source is malformed.") from exc
            if len(accounts) != len(selections):
                raise MatchStaffEntryLookupError("Every selected GameAccount must still resolve exactly once.")
            if len(characters) != len(selections):
                raise MatchStaffEntryLookupError("Every selected Umamusume must still resolve exactly once.")
            if len({account.game_region for account in accounts}) != 1:
                raise MatchStaffEntryLookupError("All Match Entries must share one game region.")
            return MatchEntryReplacementDraft(
                current=current,
                desired_entries=tuple(
                    MatchEntryDraftEntry(
                        entry_number=index,
                        account=account,
                        character=character,
                    )
                    for index, (account, character) in enumerate(zip(accounts, characters, strict=True), start=1)
                ),
            )

        return self.query_runner.run(query)

    @staticmethod
    def _require_eligible(target: MatchEntryRosterSnapshot | None) -> MatchEntryRosterSnapshot:
        if target is None:
            raise MatchStaffEntryUnavailableError("Match Entry target does not exist.")
        if target.source_kind != MatchSourceKind.NATIVE_V2 or target.status not in _EDITABLE_STATUSES:
            raise MatchStaffEntryUnavailableError("Match Entry target must be a native pre-open Match.")
        return target
