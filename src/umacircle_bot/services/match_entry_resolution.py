from __future__ import annotations

import json
from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha256
from unicodedata import normalize as unicode_normalize

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import GameAccount
from umacircle_bot.services.application import run_application_command, run_application_query
from umacircle_bot.services.dtos import RaceOperationResultDTO
from umacircle_bot.services.match_races import (
    FinalEntrySnapshotInput,
    ReplaceFinalEntrySnapshotCommand,
    replace_final_entry_snapshot,
)

MAX_MATCH_ENTRY_CANDIDATES = 10
MAX_MATCH_ENTRY_CANDIDATE_SCAN = 500
_MIN_FUZZY_SCORE = 0.35
_ENTRY_REPLACEMENT_ACTION = "room_race_entries_set"


@dataclass(frozen=True, slots=True)
class MatchEntryDraftInput:
    game_account_query: str
    character_name: str


@dataclass(frozen=True, slots=True)
class MatchEntryGameAccountCandidateDTO:
    game_account_id: int
    nickname: str | None
    ingame_name: str | None
    identity_status: str
    persona_id: str | None


@dataclass(frozen=True, slots=True)
class MatchEntryCandidateSetDTO:
    query: str
    candidates: tuple[MatchEntryGameAccountCandidateDTO, ...]


@dataclass(frozen=True, slots=True)
class ResolvedMatchEntryInput:
    game_account_id: int
    character_name: str


@dataclass(frozen=True, slots=True)
class MatchRaceResolvedEntriesCommand:
    race_id: int
    entries: tuple[ResolvedMatchEntryInput, ...]
    reason: str | None
    actor_discord_user_id: str
    interaction_id: str


def parse_match_entry_draft_text(value: str) -> tuple[MatchEntryDraftInput, ...]:
    if not isinstance(value, str):
        raise ValueError("엔트리 입력은 줄 단위 텍스트여야 합니다.")
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        raise ValueError("엔트리를 한 개 이상 입력해 주세요.")

    result: list[MatchEntryDraftInput] = []
    for line_number, line in enumerate(lines, start=1):
        if "|" not in line:
            raise ValueError(f"{line_number}번 줄은 `GameAccount 이름 일부 | 우마무스메명` 형식이어야 합니다.")
        query, character_name = (part.strip() for part in line.split("|", 1))
        if not query:
            raise ValueError(f"{line_number}번 줄의 GameAccount 검색어가 비어 있습니다.")
        if not character_name:
            raise ValueError(f"{line_number}번 줄의 우마무스메명이 비어 있습니다.")
        if len(query) > 100:
            raise ValueError(f"{line_number}번 줄의 GameAccount 검색어가 너무 깁니다.")
        if len(character_name) > 100:
            raise ValueError(f"{line_number}번 줄의 우마무스메명이 너무 깁니다.")
        result.append(MatchEntryDraftInput(game_account_query=query, character_name=character_name))
    return tuple(result)


def search_match_entry_game_account_candidates(
    session: Session,
    *,
    queries: tuple[str, ...],
    limit: int = MAX_MATCH_ENTRY_CANDIDATES,
) -> tuple[MatchEntryCandidateSetDTO, ...]:
    normalized_queries = tuple(_query_text(query) for query in queries)
    if not normalized_queries:
        return ()
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_MATCH_ENTRY_CANDIDATES:
        raise ValueError(f"candidate limit must be between 1 and {MAX_MATCH_ENTRY_CANDIDATES}")

    rows = session.execute(
        select(
            GameAccount.id,
            GameAccount.nickname,
            GameAccount.ingame_name,
            GameAccount.identity_status,
            GameAccount.persona_id,
        )
        .where(or_(GameAccount.nickname.is_not(None), GameAccount.ingame_name.is_not(None)))
        .order_by(GameAccount.id)
        .limit(MAX_MATCH_ENTRY_CANDIDATE_SCAN)
    ).all()

    return tuple(
        MatchEntryCandidateSetDTO(
            query=query,
            candidates=_rank_candidates(query, rows=rows, limit=limit),
        )
        for query in normalized_queries
    )


def query_match_entry_game_account_candidates(
    queries: tuple[str, ...],
    *,
    limit: int = MAX_MATCH_ENTRY_CANDIDATES,
) -> tuple[MatchEntryCandidateSetDTO, ...]:
    return run_application_query(
        lambda session: search_match_entry_game_account_candidates(
            session,
            queries=queries,
            limit=limit,
        )
    )


def execute_match_race_resolved_entries_replacement(
    command: MatchRaceResolvedEntriesCommand,
) -> RaceOperationResultDTO:
    if not command.entries:
        raise ValueError("resolved entry snapshot must contain at least one entry")
    normalized_entries = tuple(
        ResolvedMatchEntryInput(
            game_account_id=_positive_id(entry.game_account_id),
            character_name=_character_name(entry.character_name),
        )
        for entry in command.entries
    )
    account_ids = tuple(entry.game_account_id for entry in normalized_entries)
    if len(account_ids) != len(set(account_ids)):
        raise ValueError("같은 GameAccount를 한 Race에 두 번 등록할 수 없습니다.")
    request_fingerprint = _resolved_entry_request_fingerprint(
        race_id=command.race_id,
        actor_discord_user_id=command.actor_discord_user_id,
        entries=normalized_entries,
    )

    def operation(session: Session) -> RaceOperationResultDTO:
        rows = session.execute(
            select(GameAccount.id, GameAccount.nickname, GameAccount.ingame_name)
            .where(GameAccount.id.in_(account_ids))
            .order_by(GameAccount.id)
        ).all()
        account_by_id = {row.id: row for row in rows}
        if set(account_ids) != set(account_by_id):
            raise ValueError("선택한 GameAccount 중 더 이상 존재하지 않는 계정이 있습니다.")

        entries = tuple(
            FinalEntrySnapshotInput(
                entry_number=index,
                display_name=_display_name(account_by_id[entry.game_account_id]),
                game_account_id=entry.game_account_id,
                character_name=entry.character_name,
            )
            for index, entry in enumerate(normalized_entries, start=1)
        )
        return replace_final_entry_snapshot(
            session,
            command=ReplaceFinalEntrySnapshotCommand(
                race_id=command.race_id,
                entries=entries,
                actor_discord_user_id=command.actor_discord_user_id,
                idempotency_key=f"race-control:{command.interaction_id}",
                reason=command.reason,
                request_fingerprint=request_fingerprint,
            ),
        )

    return run_application_command(operation)


def _rank_candidates(query: str, *, rows: list[object], limit: int) -> tuple[MatchEntryGameAccountCandidateDTO, ...]:
    normalized_query = _normalize(query)
    scored: list[tuple[float, int, MatchEntryGameAccountCandidateDTO]] = []
    for row in rows:
        names = tuple(name for name in (row.nickname, row.ingame_name) if isinstance(name, str) and name.strip())
        if not names:
            continue
        score = max(_similarity(normalized_query, _normalize(name)) for name in names)
        if score < _MIN_FUZZY_SCORE:
            continue
        candidate = MatchEntryGameAccountCandidateDTO(
            game_account_id=row.id,
            nickname=row.nickname,
            ingame_name=row.ingame_name,
            identity_status=row.identity_status,
            persona_id=row.persona_id,
        )
        scored.append((score, row.id, candidate))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return tuple(item[2] for item in scored[:limit])


def _resolved_entry_request_fingerprint(
    *,
    race_id: int,
    actor_discord_user_id: str,
    entries: tuple[ResolvedMatchEntryInput, ...],
) -> str:
    payload = {
        "race_id": race_id,
        "actor": actor_discord_user_id.strip(),
        "entries": [
            {
                "entry_number": index,
                "game_account_id": entry.game_account_id,
                "character_name": entry.character_name,
            }
            for index, entry in enumerate(entries, start=1)
        ],
    }
    encoded = json.dumps(
        {"action": _ENTRY_REPLACEMENT_ACTION, "payload": payload},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _similarity(query: str, candidate: str) -> float:
    if query == candidate:
        return 3.0
    if query in candidate:
        return 2.0 + min(1.0, len(query) / max(len(candidate), 1))
    if candidate in query:
        return 1.5 + min(0.5, len(candidate) / max(len(query), 1) / 2)
    return SequenceMatcher(None, query, candidate).ratio()


def _normalize(value: str) -> str:
    return " ".join(unicode_normalize("NFKC", value).casefold().split())


def _query_text(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("GameAccount query must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > 100:
        raise ValueError("GameAccount query must contain between 1 and 100 characters")
    return normalized


def _character_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("character name must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > 100:
        raise ValueError("character name must contain between 1 and 100 characters")
    return normalized


def _positive_id(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("GameAccount ID must be a positive integer")
    return value


def _display_name(row: object) -> str:
    for value in (row.ingame_name, row.nickname):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"GameAccount #{row.id}"
