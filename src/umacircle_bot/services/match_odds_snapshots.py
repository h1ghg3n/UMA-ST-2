from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    Bet,
    MatchOddsSnapshot,
    MatchOddsSnapshotEntry,
    MatchResultSubmission,
    Race,
    RaceEntry,
    RaceOperationAudit,
    RaceResult,
)
from umacircle_bot.domain.betting import (
    MatchBetType,
    MatchPayout,
    adjust_match_payout_rate,
    match_payout_multiplier,
    normalize_match_bet_numbers,
    normalize_match_bet_type,
)
from umacircle_bot.domain.errors import MatchOddsSnapshotConflictError, MatchOddsSnapshotError
from umacircle_bot.domain.match_results import MatchResultSubmissionStatus
from umacircle_bot.domain.races import MatchRaceStatus
from umacircle_bot.domain.time import database_datetime_as_utc

ODDS_SNAPSHOT_CAPABILITY = "settlement.odds.confirm"
ODDS_SNAPSHOT_ACTION = "room_match_odds_snapshot_confirm"
RATE_QUANTUM = Decimal("0.01")
_SELECTION_COUNTS = {
    MatchBetType.WIN.value: 1,
    MatchBetType.QUINELLA.value: 2,
    MatchBetType.TRIO.value: 3,
}


@dataclass(frozen=True, slots=True)
class MatchOddsInput:
    bet_type: str
    declared_payout_rate: Decimal


@dataclass(frozen=True, slots=True)
class ConfirmMatchOddsSnapshotCommand:
    race_id: int
    odds: tuple[MatchOddsInput, ...]
    actor_discord_user_id: str
    idempotency_key: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class MatchOddsSnapshotEntryDTO:
    bet_type: str
    winning_numbers: tuple[int, ...]
    declared_payout_rate: Decimal
    effective_payout_rate: Decimal


@dataclass(frozen=True, slots=True)
class MatchOddsSnapshotDTO:
    id: int
    race_id: int
    match_result_submission_id: int
    settlement_participant_count: int
    payout_multiplier: Decimal
    confirmed_by_discord_user_id: str
    confirmed_at: datetime
    entries: tuple[MatchOddsSnapshotEntryDTO, ...]


@dataclass(frozen=True, slots=True)
class MatchOddsSnapshotOperationDTO:
    action: str
    audit_id: int
    snapshot: MatchOddsSnapshotDTO


def confirm_match_odds_snapshot(
    session: Session,
    *,
    command: ConfirmMatchOddsSnapshotCommand,
) -> MatchOddsSnapshotOperationDTO:
    race_id = _positive_int(command.race_id, field="race ID")
    actor = _text(command.actor_discord_user_id, field="actor ID", maximum=32)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    reason = _optional_text(command.reason, field="reason", maximum=255)
    odds = _normalize_odds(command.odds)
    fingerprint = _fingerprint(
        ODDS_SNAPSHOT_ACTION,
        {
            "race_id": race_id,
            "actor": actor,
            "odds": [
                {"bet_type": bet_type, "declared_payout_rate": format(payout_rate, "f")}
                for bet_type, payout_rate in odds.items()
            ],
            "reason": reason,
        },
    )

    try:
        with session.begin_nested():
            existing_audit = _load_audit(session, request_key, lock=True)
            if existing_audit is not None:
                return _idempotent_result(session, existing_audit, fingerprint=fingerprint)

            race = session.scalar(select(Race).where(Race.id == race_id).with_for_update())
            if race is None or race.race_kind != "room_match":
                raise MatchOddsSnapshotError("odds snapshot requires an existing room-match race")
            if race.external_source is not None:
                raise MatchOddsSnapshotConflictError("imported historical races are read-only")
            if race.status != MatchRaceStatus.RESULT_CONFIRMED.value:
                raise MatchOddsSnapshotError("odds snapshot requires a result-confirmed room-match race")
            submission = _load_confirmed_submission(session, race_id=race.id)
            existing = session.scalar(
                select(MatchOddsSnapshot).where(MatchOddsSnapshot.race_id == race.id).with_for_update()
            )
            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    raise MatchOddsSnapshotConflictError("room-match odds snapshot is already immutable")
                return _snapshot_operation(session, snapshot=existing, audit_id=0)

            settlement_count, finishers_by_rank = _settlement_result_snapshot(session, race_id=race.id)
            expected_bet_types = _active_bet_types(session, race_id=race.id)
            if set(odds) != expected_bet_types:
                raise MatchOddsSnapshotError("odds input must cover exactly the active room-match bet types")
            multiplier = match_payout_multiplier(settlement_count)
            snapshot = MatchOddsSnapshot(
                race_id=race.id,
                match_result_submission_id=submission.id,
                settlement_participant_count=settlement_count,
                payout_multiplier=multiplier,
                confirmed_by_discord_user_id=actor,
                idempotency_key=request_key,
                request_fingerprint=fingerprint,
            )
            session.add(snapshot)
            session.flush()
            entries = [
                MatchOddsSnapshotEntry(
                    odds_snapshot_id=snapshot.id,
                    bet_type=bet_type,
                    winning_numbers=_winning_numbers(bet_type, finishers_by_rank),
                    declared_payout_rate=payout_rate,
                    effective_payout_rate=_effective_rate(payout_rate, settlement_count),
                )
                for bet_type, payout_rate in odds.items()
            ]
            session.add_all(entries)
            session.flush()
            audit = RaceOperationAudit(
                race_id=race.id,
                action=ODDS_SNAPSHOT_ACTION,
                capability=ODDS_SNAPSHOT_CAPABILITY,
                actor_discord_user_id=actor,
                idempotency_key=request_key,
                request_fingerprint=fingerprint,
                before_json={
                    "race_status": race.status,
                    "match_result_submission_id": submission.id,
                },
                after_json={"snapshot_id": snapshot.id, "snapshot": _snapshot_json(session, snapshot)},
                reason=reason,
            )
            session.add(audit)
            session.flush()
            return _snapshot_operation(session, snapshot=snapshot, audit_id=audit.id)
    except IntegrityError as exc:
        with session.begin_nested():
            existing_audit = _load_audit(session, request_key, lock=True)
            if existing_audit is None:
                raise exc
            return _idempotent_result(session, existing_audit, fingerprint=fingerprint)


def get_match_odds_snapshot(
    session: Session,
    *,
    race_id: int,
) -> MatchOddsSnapshotDTO | None:
    normalized_race_id = _positive_int(race_id, field="race ID")
    snapshot = session.scalar(
        select(MatchOddsSnapshot).where(MatchOddsSnapshot.race_id == normalized_race_id).order_by(MatchOddsSnapshot.id)
    )
    return _snapshot_dto(session, snapshot) if snapshot is not None else None


def _normalize_odds(value: tuple[MatchOddsInput, ...]) -> dict[str, Decimal]:
    if not isinstance(value, tuple):
        raise MatchOddsSnapshotError("odds input must be a tuple")
    normalized: dict[str, Decimal] = {}
    for item in value:
        if not isinstance(item, MatchOddsInput):
            raise MatchOddsSnapshotError("odds input contains an invalid item")
        bet_type = normalize_match_bet_type(item.bet_type).value
        try:
            validated = MatchPayout(payout_amount=0, payout_rate=item.declared_payout_rate)
        except ValueError as exc:
            raise MatchOddsSnapshotError(str(exc)) from exc
        assert validated.payout_rate is not None
        if bet_type in normalized:
            raise MatchOddsSnapshotError("odds input must not duplicate a bet type")
        normalized[bet_type] = validated.payout_rate.quantize(RATE_QUANTUM)
    return dict(sorted(normalized.items()))


def _load_confirmed_submission(session: Session, *, race_id: int) -> MatchResultSubmission:
    submission = session.scalar(
        select(MatchResultSubmission)
        .where(
            MatchResultSubmission.race_id == race_id,
            MatchResultSubmission.current_marker == "current",
        )
        .with_for_update()
    )
    if submission is None or submission.submission_status != MatchResultSubmissionStatus.CONFIRMED.value:
        raise MatchOddsSnapshotError("odds snapshot requires the current confirmed result submission")
    return submission


def _settlement_result_snapshot(session: Session, *, race_id: int) -> tuple[int, dict[int, int]]:
    entries = set(
        session.scalars(
            select(RaceEntry.entry_number)
            .where(RaceEntry.race_id == race_id, RaceEntry.entry_kind == "room_match")
            .with_for_update()
        )
    )
    if not entries:
        raise MatchOddsSnapshotError("odds snapshot requires the final room-match entry snapshot")
    results = list(
        session.scalars(
            select(RaceResult)
            .where(RaceResult.race_id == race_id)
            .order_by(RaceResult.rank, RaceResult.id)
            .with_for_update()
        )
    )
    eligible = [result for result in results if not result.is_betting_excluded and not result.is_result_void]
    if not eligible:
        raise MatchOddsSnapshotError("odds snapshot requires settlement-eligible results")
    finishers_by_rank: dict[int, int] = {}
    for result in eligible:
        if result.entry_number not in entries or result.rank <= 0:
            raise MatchOddsSnapshotError("confirmed results do not match the room-match entry snapshot")
        if result.rank in finishers_by_rank:
            raise MatchOddsSnapshotError("confirmed results contain duplicate settlement ranks")
        finishers_by_rank[result.rank] = result.entry_number
    return len(eligible), finishers_by_rank


def _active_bet_types(session: Session, *, race_id: int) -> set[str]:
    return set(
        session.scalars(
            select(Bet.bet_type)
            .where(
                Bet.race_id == race_id,
                Bet.betting_mode == "room_match",
                Bet.status == "active",
            )
            .order_by(Bet.bet_type)
            .with_for_update()
        )
    )


def _winning_numbers(bet_type: str, finishers_by_rank: Mapping[int, int]) -> list[int]:
    count = _SELECTION_COUNTS[bet_type]
    try:
        values = [finishers_by_rank[rank] for rank in range(1, count + 1)]
    except KeyError as exc:
        raise MatchOddsSnapshotError("confirmed results do not cover every active bet type") from exc
    return normalize_match_bet_numbers(bet_type, values)


def _effective_rate(declared_rate: Decimal, participant_count: int) -> Decimal:
    return adjust_match_payout_rate(declared_rate, participant_count).quantize(RATE_QUANTUM)


def _load_audit(session: Session, key: str, *, lock: bool) -> RaceOperationAudit | None:
    query = select(RaceOperationAudit).where(RaceOperationAudit.idempotency_key == key)
    if lock:
        query = query.with_for_update()
    return session.scalar(query)


def _idempotent_result(
    session: Session,
    audit: RaceOperationAudit,
    *,
    fingerprint: str,
) -> MatchOddsSnapshotOperationDTO:
    if audit.action != ODDS_SNAPSHOT_ACTION or audit.request_fingerprint != fingerprint:
        raise MatchOddsSnapshotConflictError("idempotency key payload does not match the original odds snapshot")
    snapshot_id = audit.after_json.get("snapshot_id")
    if not isinstance(snapshot_id, int) or isinstance(snapshot_id, bool) or snapshot_id <= 0:
        raise MatchOddsSnapshotConflictError("stored odds snapshot audit is invalid")
    snapshot = session.get(MatchOddsSnapshot, snapshot_id)
    if snapshot is None:
        raise MatchOddsSnapshotConflictError("stored odds snapshot is missing")
    return _snapshot_operation(session, snapshot=snapshot, audit_id=audit.id)


def _snapshot_operation(
    session: Session,
    *,
    snapshot: MatchOddsSnapshot,
    audit_id: int,
) -> MatchOddsSnapshotOperationDTO:
    return MatchOddsSnapshotOperationDTO(
        action=ODDS_SNAPSHOT_ACTION,
        audit_id=audit_id,
        snapshot=_snapshot_dto(session, snapshot),
    )


def _snapshot_dto(session: Session, snapshot: MatchOddsSnapshot) -> MatchOddsSnapshotDTO:
    entries = tuple(
        MatchOddsSnapshotEntryDTO(
            bet_type=entry.bet_type,
            winning_numbers=tuple(entry.winning_numbers),
            declared_payout_rate=entry.declared_payout_rate,
            effective_payout_rate=entry.effective_payout_rate,
        )
        for entry in session.scalars(
            select(MatchOddsSnapshotEntry)
            .where(MatchOddsSnapshotEntry.odds_snapshot_id == snapshot.id)
            .order_by(MatchOddsSnapshotEntry.bet_type)
        )
    )
    return MatchOddsSnapshotDTO(
        id=snapshot.id,
        race_id=snapshot.race_id,
        match_result_submission_id=snapshot.match_result_submission_id,
        settlement_participant_count=snapshot.settlement_participant_count,
        payout_multiplier=snapshot.payout_multiplier,
        confirmed_by_discord_user_id=snapshot.confirmed_by_discord_user_id,
        confirmed_at=database_datetime_as_utc(snapshot.confirmed_at),
        entries=entries,
    )


def _snapshot_json(session: Session, snapshot: MatchOddsSnapshot) -> dict[str, Any]:
    dto = _snapshot_dto(session, snapshot)
    return {
        "id": dto.id,
        "race_id": dto.race_id,
        "match_result_submission_id": dto.match_result_submission_id,
        "settlement_participant_count": dto.settlement_participant_count,
        "payout_multiplier": format(dto.payout_multiplier, "f"),
        "confirmed_by_discord_user_id": dto.confirmed_by_discord_user_id,
        "entries": [
            {
                "bet_type": entry.bet_type,
                "winning_numbers": list(entry.winning_numbers),
                "declared_payout_rate": format(entry.declared_payout_rate, "f"),
                "effective_payout_rate": format(entry.effective_payout_rate, "f"),
            }
            for entry in dto.entries
        ],
    }


def _fingerprint(action: str, value: Mapping[str, Any]) -> str:
    payload = json.dumps({"action": action, **value}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _positive_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise MatchOddsSnapshotError(f"{field} must be a positive integer")
    return value


def _text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise MatchOddsSnapshotError(f"{field} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or any(ord(character) < 32 for character in normalized):
        raise MatchOddsSnapshotError(f"{field} must contain 1 to {maximum} printable characters")
    return normalized


def _optional_text(value: object, *, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, field=field, maximum=maximum)
