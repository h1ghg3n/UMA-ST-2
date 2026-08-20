from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    Bet,
    CirclePointTransaction,
    MatchOddsSnapshot,
    MatchOddsSnapshotEntry,
    MatchResultSubmission,
    Race,
    RaceCondition,
    RaceOperationAudit,
    RaceRatingContext,
    RaceResult,
    RatingEvent,
)
from umacircle_bot.domain.betting import (
    MatchPayout,
    calculate_match_payout_amount_from_effective_rate,
)
from umacircle_bot.domain.errors import MatchOddsSnapshotError, MatchSettlementConflictError
from umacircle_bot.domain.match_results import MatchResultSubmissionStatus
from umacircle_bot.domain.races import MatchRaceStatus
from umacircle_bot.domain.time import database_datetime_as_utc
from umacircle_bot.services.betting import judge_match_bets, settle_match_bets
from umacircle_bot.services.dtos import BetJudgementDTO, CirclePointTransactionDTO
from umacircle_bot.services.match_odds_snapshots import (
    MatchOddsSnapshotDTO,
    get_match_odds_snapshot,
)
from umacircle_bot.services.match_placement_settlement import (
    PLACEMENT_TRANSACTION_SOURCE,
    PLACEMENT_TRANSACTION_TYPE,
    NativePlacementPlan,
    build_native_placement_plan,
    create_native_placement_rewards,
)
from umacircle_bot.services.match_rating import (
    MatchRatingResult,
    lock_rating_write_gate,
    normalize_rating_grade,
    record_match_rating,
)
from umacircle_bot.services.persona_wallets import lock_persona_wallets

SETTLEMENT_CONFIRM_ACTION = "room_match_settlement_confirm"
SETTLEMENT_CONFIRM_CAPABILITY = "settlement.confirm"


@dataclass(frozen=True, slots=True)
class ConfirmMatchSettlementCommand:
    race_id: int
    actor_discord_user_id: str
    idempotency_key: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class MatchSettlementOperationDTO:
    action: str
    audit_id: int
    race_id: int
    race_status: str
    odds_snapshot: MatchOddsSnapshotDTO
    rating: MatchRatingResult
    transactions: tuple[CirclePointTransactionDTO, ...]


def confirm_match_settlement(
    session: Session,
    *,
    command: ConfirmMatchSettlementCommand,
) -> MatchSettlementOperationDTO:
    race_id = _positive_int(command.race_id, field="race ID")
    actor = _discord_user_id(command.actor_discord_user_id)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    reason = _optional_text(command.reason, field="reason", maximum=255)
    fingerprint = _fingerprint(
        SETTLEMENT_CONFIRM_ACTION,
        {"race_id": race_id, "actor": actor, "reason": reason},
    )

    try:
        with session.begin_nested():
            lock_rating_write_gate(session)
            race = session.scalar(select(Race).where(Race.id == race_id).with_for_update())
            existing_audit = _load_audit(session, request_key, lock=True)
            if existing_audit is not None:
                return _idempotent_result(session, existing_audit, fingerprint=fingerprint)
            if race is None or race.race_kind != "room_match":
                raise MatchSettlementConflictError("settlement requires an existing room-match race")
            if race.external_source is not None:
                raise MatchSettlementConflictError("imported historical races are read-only")
            if race.status != MatchRaceStatus.RESULT_CONFIRMED.value:
                raise MatchSettlementConflictError("settlement requires a result-confirmed room-match race")
            snapshot = _load_current_snapshot(session, race_id=race.id)
            odds_snapshot = get_match_odds_snapshot(session, race_id=race.id)
            if odds_snapshot is None:
                raise MatchOddsSnapshotError("settlement requires an immutable odds snapshot")
            snapshot_entries = _snapshot_entries(session, snapshot_id=snapshot.id)

            judgements = judge_match_bets(
                session,
                race_id=race.id,
                judged_by_discord_user_id=actor,
            )
            payouts = _snapshot_payouts(snapshot_entries, judgements)
            placement_plan = build_native_placement_plan(session, race_id=race.id)
            settlement_basis_checksum = _settlement_basis_checksum(
                snapshot,
                snapshot_entries=snapshot_entries,
                placement_plan=placement_plan,
            )
            wallet_persona_ids = set(placement_plan.persona_ids)
            wallet_persona_ids.update(_bet_persona_ids(session, race_id=race.id))
            wallets_by_persona = lock_persona_wallets(
                session,
                persona_ids=wallet_persona_ids,
            )
            rating = record_match_rating(session, race_id=race.id)
            bet_transactions = settle_match_bets(
                session,
                race_id=race.id,
                payouts_by_bet_id=payouts,
                settled_by_discord_user_id=actor,
                payout_rates_are_effective=True,
                locked_wallets_by_persona=wallets_by_persona,
            )
            placement_rows = create_native_placement_rewards(
                session,
                plan=placement_plan,
                wallets_by_persona=wallets_by_persona,
                actor_discord_user_id=actor,
            )
            placement_transactions = tuple(_transaction_dto(row) for row in placement_rows)
            transactions = bet_transactions + placement_transactions
            race.status = MatchRaceStatus.SETTLED.value
            session.flush()
            rating_event_ids = list(
                session.scalars(select(RatingEvent.id).where(RatingEvent.race_id == race.id).order_by(RatingEvent.id))
            )
            audit = RaceOperationAudit(
                race_id=race.id,
                action=SETTLEMENT_CONFIRM_ACTION,
                capability=SETTLEMENT_CONFIRM_CAPABILITY,
                actor_discord_user_id=actor,
                idempotency_key=request_key,
                request_fingerprint=fingerprint,
                before_json={
                    "race_status": MatchRaceStatus.RESULT_CONFIRMED.value,
                    "odds_snapshot_id": snapshot.id,
                },
                after_json={
                    "race_status": race.status,
                    "odds_snapshot_id": snapshot.id,
                    "rating_rule_version_id": rating.rating_rule_version_id,
                    "rating_event_ids": rating_event_ids,
                    "settlement_transaction_ids": [transaction.id for transaction in bet_transactions],
                    "bet_transaction_ids": [transaction.id for transaction in bet_transactions],
                    "placement_transaction_ids": [transaction.id for transaction in placement_transactions],
                    "placement_selections": placement_plan.audit_selections(),
                    "placement_policy_checksum": placement_plan.policy_checksum,
                    "placement_result_snapshot_checksum": placement_plan.result_snapshot_checksum,
                    "settlement_basis_checksum": settlement_basis_checksum,
                },
                reason=reason,
            )
            session.add(audit)
            session.flush()
            return MatchSettlementOperationDTO(
                action=SETTLEMENT_CONFIRM_ACTION,
                audit_id=audit.id,
                race_id=race.id,
                race_status=race.status,
                odds_snapshot=odds_snapshot,
                rating=rating,
                transactions=transactions,
            )
    except IntegrityError:
        with session.begin_nested():
            # Keep the same lock order as the normal path when recovering a
            # concurrent unique-key race: Rating gate -> target Race -> audit.
            lock_rating_write_gate(session)
            session.scalar(select(Race.id).where(Race.id == race_id).with_for_update())
            existing_audit = _load_audit(session, request_key, lock=True)
            if existing_audit is None:
                raise
            return _idempotent_result(session, existing_audit, fingerprint=fingerprint)


def _load_current_snapshot(session: Session, *, race_id: int) -> MatchOddsSnapshot:
    submission = session.scalar(
        select(MatchResultSubmission)
        .where(
            MatchResultSubmission.race_id == race_id,
            MatchResultSubmission.current_marker == "current",
        )
        .with_for_update()
    )
    if submission is None or submission.submission_status != MatchResultSubmissionStatus.CONFIRMED.value:
        raise MatchSettlementConflictError("settlement requires the current confirmed result submission")
    snapshot = session.scalar(select(MatchOddsSnapshot).where(MatchOddsSnapshot.race_id == race_id).with_for_update())
    if snapshot is None:
        raise MatchOddsSnapshotError("settlement requires an immutable odds snapshot")
    if snapshot.match_result_submission_id != submission.id:
        raise MatchSettlementConflictError("odds snapshot does not belong to the current confirmed result")
    return snapshot


def _snapshot_payouts(
    snapshot_entries: tuple[MatchOddsSnapshotEntry, ...],
    judgements: tuple[BetJudgementDTO, ...],
) -> dict[int, MatchPayout]:
    entries_by_bet_type = {entry.bet_type: entry for entry in snapshot_entries}
    payouts: dict[int, MatchPayout] = {}
    for judgement in judgements:
        if judgement.judgement_status != "hit":
            continue
        entry = entries_by_bet_type.get(judgement.bet_type)
        if entry is None or tuple(entry.winning_numbers) != judgement.selected_entry_numbers:
            raise MatchOddsSnapshotError("odds snapshot does not cover the confirmed winning bet selection")
        payout_amount = calculate_match_payout_amount_from_effective_rate(
            judgement.stake_amount,
            entry.effective_payout_rate,
        )
        payouts[judgement.bet_id] = MatchPayout(
            payout_amount=payout_amount,
            payout_rate=entry.effective_payout_rate,
        )
    return payouts


def _snapshot_entries(session: Session, *, snapshot_id: int) -> tuple[MatchOddsSnapshotEntry, ...]:
    return tuple(
        session.scalars(
            select(MatchOddsSnapshotEntry)
            .where(MatchOddsSnapshotEntry.odds_snapshot_id == snapshot_id)
            .order_by(MatchOddsSnapshotEntry.bet_type)
            .with_for_update()
        )
    )


def _idempotent_result(
    session: Session,
    audit: RaceOperationAudit,
    *,
    fingerprint: str,
) -> MatchSettlementOperationDTO:
    if audit.action != SETTLEMENT_CONFIRM_ACTION or audit.request_fingerprint != fingerprint:
        raise MatchSettlementConflictError("idempotency key payload does not match the original settlement")
    race = session.scalar(select(Race).where(Race.id == audit.race_id).with_for_update())
    if race is None or race.status != MatchRaceStatus.SETTLED.value:
        raise MatchSettlementConflictError("stored settlement state is inconsistent")
    snapshot = get_match_odds_snapshot(session, race_id=race.id)
    if snapshot is None:
        raise MatchSettlementConflictError("stored settlement odds snapshot is missing")
    snapshot_row = _load_current_snapshot(session, race_id=race.id)
    snapshot_entries = _snapshot_entries(session, snapshot_id=snapshot_row.id)
    placement_plan = build_native_placement_plan(session, race_id=race.id)
    after = audit.after_json
    if not isinstance(after, dict):
        raise MatchSettlementConflictError("stored settlement audit payload is invalid")
    if after.get("settlement_basis_checksum") != _settlement_basis_checksum(
        snapshot_row,
        snapshot_entries=snapshot_entries,
        placement_plan=placement_plan,
    ):
        raise MatchSettlementConflictError("stored settlement basis no longer matches the confirmed snapshot")
    if (
        after.get("placement_policy_checksum") != placement_plan.policy_checksum
        or after.get("placement_result_snapshot_checksum") != placement_plan.result_snapshot_checksum
        or after.get("placement_selections") != placement_plan.audit_selections()
    ):
        raise MatchSettlementConflictError("stored settlement placement provenance is inconsistent")
    rating = _rating_from_state(session, race_id=race.id)
    rating_event_ids = list(
        session.scalars(select(RatingEvent.id).where(RatingEvent.race_id == race.id).order_by(RatingEvent.id))
    )
    if after.get("rating_event_ids") != rating_event_ids:
        raise MatchSettlementConflictError("stored settlement Rating event coverage is inconsistent")
    bet_transaction_ids = _audit_integer_ids(after, "bet_transaction_ids")
    if after.get("settlement_transaction_ids") != bet_transaction_ids:
        raise MatchSettlementConflictError("stored settlement Bet transaction coverage is inconsistent")
    placement_transaction_ids = _audit_integer_ids(after, "placement_transaction_ids")
    transactions = _settlement_transactions_from_audit(
        session,
        race_id=race.id,
        bet_transaction_ids=bet_transaction_ids,
        placement_transaction_ids=placement_transaction_ids,
        placement_plan=placement_plan,
    )
    return MatchSettlementOperationDTO(
        action=SETTLEMENT_CONFIRM_ACTION,
        audit_id=audit.id,
        race_id=race.id,
        race_status=race.status,
        odds_snapshot=snapshot,
        rating=rating,
        transactions=transactions,
    )


def _rating_from_state(session: Session, *, race_id: int) -> MatchRatingResult:
    condition = session.scalar(select(RaceCondition).where(RaceCondition.race_id == race_id))
    if condition is None:
        raise MatchSettlementConflictError("stored settlement race conditions are missing")
    grade = normalize_rating_grade(condition.grade)
    events = list(session.scalars(select(RatingEvent).where(RatingEvent.race_id == race_id).order_by(RatingEvent.id)))
    if grade == "OP":
        if events:
            raise MatchSettlementConflictError("OP settlement must not have Rating events")
        return MatchRatingResult(
            race_id=race_id,
            rating_rule_version_id=None,
            grade=grade,
            participant_count=condition.participant_count,
            event_count=0,
            average_rating_before=None,
        )
    context = session.scalar(select(RaceRatingContext).where(RaceRatingContext.race_id == race_id))
    if context is None or not events:
        raise MatchSettlementConflictError("stored settlement Rating state is incomplete")
    version_ids = {event.rating_rule_version_id for event in events}
    if len(version_ids) != 1 or None in version_ids:
        raise MatchSettlementConflictError("stored settlement Rating rule version is inconsistent")
    return MatchRatingResult(
        race_id=race_id,
        rating_rule_version_id=next(iter(version_ids)),
        grade=grade,
        participant_count=condition.participant_count,
        event_count=len(events),
        average_rating_before=context.average_rating_before,
    )


def _settlement_transactions_from_audit(
    session: Session,
    *,
    race_id: int,
    bet_transaction_ids: list[int],
    placement_transaction_ids: list[int],
    placement_plan: NativePlacementPlan,
) -> tuple[CirclePointTransactionDTO, ...]:
    transaction_ids = bet_transaction_ids + placement_transaction_ids
    if len(transaction_ids) != len(set(transaction_ids)):
        raise MatchSettlementConflictError("stored settlement transaction IDs overlap")
    rows = (
        tuple(
            session.scalars(
                select(CirclePointTransaction)
                .where(CirclePointTransaction.id.in_(transaction_ids))
                .order_by(CirclePointTransaction.id)
                .with_for_update()
            )
        )
        if transaction_ids
        else ()
    )
    rows_by_id = {row.id: row for row in rows}
    if set(rows_by_id) != set(transaction_ids):
        raise MatchSettlementConflictError("stored settlement transaction set is incomplete")

    bet_rows = [rows_by_id[row_id] for row_id in bet_transaction_ids]
    bet_ids = [row.related_bet_id for row in bet_rows]
    if any(
        row.type != "settlement_reward"
        or row.source != "room_match_settlement"
        or row.related_bet_id is None
        or row.related_race_result_id is not None
        for row in bet_rows
    ):
        raise MatchSettlementConflictError("stored settlement Bet transaction provenance is inconsistent")
    stored_bet_race_ids = (
        tuple(session.scalars(select(Bet.race_id).where(Bet.id.in_(bet_ids)).order_by(Bet.id))) if bet_ids else ()
    )
    if len(stored_bet_race_ids) != len(bet_ids) or (bet_ids and set(stored_bet_race_ids) != {race_id}):
        raise MatchSettlementConflictError("stored settlement Bet transaction Race coverage is inconsistent")
    extra_bet_transaction_ids = tuple(
        session.scalars(
            select(CirclePointTransaction.id)
            .join(Bet, Bet.id == CirclePointTransaction.related_bet_id)
            .where(
                Bet.race_id == race_id,
                CirclePointTransaction.type == "settlement_reward",
            )
            .order_by(CirclePointTransaction.id)
            .with_for_update()
        )
    )
    if set(extra_bet_transaction_ids) != set(bet_transaction_ids):
        raise MatchSettlementConflictError("stored settlement Bet transaction audit coverage is inconsistent")

    placement_rows = [rows_by_id[row_id] for row_id in placement_transaction_ids]
    placement_by_result_id = {row.related_race_result_id: row for row in placement_rows}
    if len(placement_by_result_id) != len(placement_rows):
        raise MatchSettlementConflictError("stored placement transaction Result coverage is inconsistent")
    for selection in placement_plan.selections:
        row = placement_by_result_id.get(selection.result_id)
        if (
            row is None
            or row.type != PLACEMENT_TRANSACTION_TYPE
            or row.source != PLACEMENT_TRANSACTION_SOURCE
            or row.related_bet_id is not None
            or row.persona_id != selection.owner_at_event_persona_id
            or row.game_account_id != selection.game_account_id
            or row.amount != selection.amount
        ):
            raise MatchSettlementConflictError("stored placement transaction provenance is inconsistent")
    if set(placement_by_result_id) != {selection.result_id for selection in placement_plan.selections}:
        raise MatchSettlementConflictError("stored placement transaction set is inconsistent")

    race_result_ids = tuple(
        session.scalars(select(RaceResult.id).where(RaceResult.race_id == race_id).order_by(RaceResult.id))
    )
    extra_placement_ids = (
        tuple(
            session.scalars(
                select(CirclePointTransaction.id)
                .where(
                    CirclePointTransaction.related_race_result_id.in_(race_result_ids),
                    CirclePointTransaction.type == PLACEMENT_TRANSACTION_TYPE,
                )
                .order_by(CirclePointTransaction.id)
                .with_for_update()
            )
        )
        if race_result_ids
        else ()
    )
    if set(extra_placement_ids) != set(placement_transaction_ids):
        raise MatchSettlementConflictError("stored placement transaction audit coverage is inconsistent")
    return tuple(_transaction_dto(rows_by_id[row_id]) for row_id in transaction_ids)


def _bet_persona_ids(session: Session, *, race_id: int) -> tuple[str, ...]:
    return tuple(
        session.scalars(
            select(Bet.persona_id)
            .where(Bet.race_id == race_id, Bet.betting_mode == "room_match")
            .order_by(Bet.persona_id)
        )
    )


def _settlement_basis_checksum(
    snapshot: MatchOddsSnapshot,
    *,
    snapshot_entries: tuple[MatchOddsSnapshotEntry, ...],
    placement_plan: NativePlacementPlan,
) -> str:
    return _fingerprint(
        "room_match_settlement_basis",
        {
            "odds_snapshot": {
                "id": snapshot.id,
                "race_id": snapshot.race_id,
                "match_result_submission_id": snapshot.match_result_submission_id,
                "settlement_participant_count": snapshot.settlement_participant_count,
                "payout_multiplier": format(snapshot.payout_multiplier, "f"),
                "entries": [
                    {
                        "bet_type": entry.bet_type,
                        "winning_numbers": entry.winning_numbers,
                        "declared_payout_rate": format(entry.declared_payout_rate, "f"),
                        "effective_payout_rate": format(entry.effective_payout_rate, "f"),
                    }
                    for entry in snapshot_entries
                ],
            },
            "placement": {
                "policy_checksum": placement_plan.policy_checksum,
                "result_snapshot_checksum": placement_plan.result_snapshot_checksum,
                "selections": placement_plan.audit_selections(),
            },
        },
    )


def _audit_integer_ids(after: Mapping[str, Any], field: str) -> list[int]:
    value = after.get(field)
    if (
        not isinstance(value, list)
        or any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in value)
        or len(value) != len(set(value))
    ):
        raise MatchSettlementConflictError(f"stored settlement {field} is invalid")
    return value


def _transaction_dto(row: CirclePointTransaction) -> CirclePointTransactionDTO:
    return CirclePointTransactionDTO(
        id=row.id,
        persona_id=row.persona_id,
        game_account_id=row.game_account_id,
        type=row.type,
        amount=row.amount,
        reason=row.reason,
        source=row.source,
        related_bet_id=row.related_bet_id,
        related_race_result_id=row.related_race_result_id,
        created_by_discord_user_id=row.created_by_discord_user_id,
        created_at=database_datetime_as_utc(row.created_at),
    )


def _load_audit(session: Session, key: str, *, lock: bool) -> RaceOperationAudit | None:
    query = select(RaceOperationAudit).where(RaceOperationAudit.idempotency_key == key)
    if lock:
        query = query.with_for_update()
    return session.scalar(query)


def _fingerprint(action: str, value: Mapping[str, Any]) -> str:
    payload = json.dumps({"action": action, **value}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _positive_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise MatchSettlementConflictError(f"{field} must be a positive integer")
    return value


def _discord_user_id(value: object) -> str:
    normalized = _text(value, field="actor ID", maximum=32)
    if not normalized.isascii() or not normalized.isdigit():
        raise MatchSettlementConflictError("actor ID must contain 1 to 32 ASCII digits")
    return normalized


def _text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise MatchSettlementConflictError(f"{field} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or any(ord(character) < 32 for character in normalized):
        raise MatchSettlementConflictError(f"{field} must contain 1 to {maximum} printable characters")
    return normalized


def _optional_text(value: object, *, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, field=field, maximum=maximum)
