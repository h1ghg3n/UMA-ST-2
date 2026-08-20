from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    Bet,
    BetJudgement,
    CirclePointTransaction,
    Race,
    RaceCondition,
    RaceOperationAudit,
    RaceRatingContext,
    RaceResult,
    RatingEvent,
)
from umacircle_bot.domain.betting import validate_circle_point_balance
from umacircle_bot.domain.errors import BettingRuleError, MatchSettlementConflictError
from umacircle_bot.domain.races import MatchRaceStatus
from umacircle_bot.domain.time import database_datetime_as_utc
from umacircle_bot.services.dtos import CirclePointTransactionDTO
from umacircle_bot.services.match_placement_settlement import (
    PLACEMENT_ROLLBACK_TRANSACTION_SOURCE,
    PLACEMENT_ROLLBACK_TRANSACTION_TYPE,
    PLACEMENT_TRANSACTION_SOURCE,
    PLACEMENT_TRANSACTION_TYPE,
    NativePlacementPlan,
    build_native_placement_plan,
)
from umacircle_bot.services.match_rating import lock_rating_write_gate, normalize_rating_grade
from umacircle_bot.services.match_settlement import SETTLEMENT_CONFIRM_ACTION
from umacircle_bot.services.persona_wallets import lock_persona_wallets

SETTLEMENT_ROLLBACK_ACTION = "room_match_settlement_rollback"
SETTLEMENT_ROLLBACK_CAPABILITY = "settlement.rollback"
SETTLEMENT_TRANSACTION_TYPE = "settlement_reward"
ROLLBACK_TRANSACTION_TYPE = "settlement_rollback"
STAKE_TRANSACTION_TYPE = "bet_stake"
REFUND_TRANSACTION_TYPE = "bet_refund"


@dataclass(frozen=True, slots=True)
class RollbackMatchSettlementCommand:
    race_id: int
    actor_discord_user_id: str
    idempotency_key: str
    reason: str


@dataclass(frozen=True, slots=True)
class MatchSettlementRollbackDTO:
    action: str
    audit_id: int
    race_id: int
    race_status: str
    original_settlement_audit_id: int
    transactions: tuple[CirclePointTransactionDTO, ...]
    reversed_rating_event_ids: tuple[int, ...]
    compensating_rating_event_ids: tuple[int, ...]


def rollback_confirmed_match_settlement(
    session: Session,
    *,
    command: RollbackMatchSettlementCommand,
) -> MatchSettlementRollbackDTO:
    """Terminally void one native settlement with append-only point and Rating compensation."""

    race_id = _positive_int(command.race_id, field="race ID")
    actor = _discord_user_id(command.actor_discord_user_id)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    reason = _text(command.reason, field="reason", maximum=255)
    fingerprint = _fingerprint(
        SETTLEMENT_ROLLBACK_ACTION,
        {"race_id": race_id, "actor": actor, "reason": reason},
    )

    try:
        with session.begin_nested():
            # All Rating writers use gate -> Race lock order. This also ensures
            # the events being reversed are still each account's latest event.
            lock_rating_write_gate(session)
            race = session.scalar(select(Race).where(Race.id == race_id).with_for_update())
            # A concurrent exact retry can start before the first transaction
            # inserts its audit, then wait on the Rating/Race locks. Repeat the
            # current locking read after those waits so it returns the committed
            # result instead of misclassifying the now-voided Race as a conflict.
            existing_audit = _load_audit_by_key(session, request_key, lock=True)
            if existing_audit is not None:
                return _idempotent_result(session, existing_audit, fingerprint=fingerprint)
            _validate_rollback_race(race)
            assert race is not None

            settlement_audit = _load_original_settlement_audit(session, race_id=race.id)
            placement_plan = build_native_placement_plan(session, race_id=race.id)
            placement_transactions = _load_original_placement_transactions(
                session,
                race_id=race.id,
                settlement_audit=settlement_audit,
                plan=placement_plan,
            )
            bets = tuple(
                session.scalars(
                    select(Bet)
                    .where(Bet.race_id == race.id, Bet.betting_mode == "room_match")
                    .order_by(Bet.id)
                    .with_for_update()
                )
            )
            judgements = tuple(
                session.scalars(
                    select(BetJudgement)
                    .where(BetJudgement.race_id == race.id)
                    .order_by(BetJudgement.bet_id)
                    .with_for_update()
                )
            )
            judgements_by_bet_id = _validate_judgements(bets, judgements)
            transaction_rows = _load_bet_transactions(session, bets)
            wallets_by_persona = lock_persona_wallets(
                session,
                persona_ids=({bet.persona_id for bet in bets} | {row.persona_id for row in placement_transactions}),
            )
            settlement_by_bet_id, stake_by_bet_id = _validate_transaction_history(
                bets,
                judgements_by_bet_id,
                transaction_rows,
                settlement_audit=settlement_audit,
            )
            wallet_deltas: dict[str, int] = defaultdict(int)
            for bet in bets:
                settlement = settlement_by_bet_id[bet.id]
                wallet_deltas[bet.persona_id] += bet.amount - settlement.amount
            for transaction in placement_transactions:
                wallet_deltas[transaction.persona_id] -= transaction.amount
            for persona_id, delta in wallet_deltas.items():
                wallet = wallets_by_persona[persona_id]
                try:
                    validate_circle_point_balance(wallet.balance + delta)
                except BettingRuleError as exc:
                    raise MatchSettlementConflictError(
                        "settlement rollback would exceed the supported Circle Point balance range"
                    ) from exc

            now = datetime.now(UTC)
            compensation_rows: list[CirclePointTransaction] = []
            for bet in bets:
                wallet = wallets_by_persona[bet.persona_id]
                settlement = settlement_by_bet_id[bet.id]
                wallet.balance += bet.amount - settlement.amount
                bet.status = "cancelled"
                bet.cancelled_at = now
                payout_rollback = CirclePointTransaction(
                    persona_id=bet.persona_id,
                    game_account_id=bet.game_account_id,
                    type=ROLLBACK_TRANSACTION_TYPE,
                    amount=-settlement.amount,
                    reason=f"room_match_settlement_rollback:{race.id}:{reason}",
                    source="room_match_settlement_rollback",
                    related_bet_id=bet.id,
                    created_by_discord_user_id=actor,
                )
                stake_refund = CirclePointTransaction(
                    persona_id=bet.persona_id,
                    game_account_id=bet.game_account_id,
                    type=REFUND_TRANSACTION_TYPE,
                    amount=stake_by_bet_id[bet.id].amount * -1,
                    reason=f"room_match_void_refund:{race.id}:{reason}",
                    source="room_match_settlement_rollback",
                    related_bet_id=bet.id,
                    created_by_discord_user_id=actor,
                )
                session.add_all((payout_rollback, stake_refund))
                compensation_rows.extend((payout_rollback, stake_refund))

            placement_compensation_rows: list[CirclePointTransaction] = []
            for original in placement_transactions:
                wallets_by_persona[original.persona_id].balance -= original.amount
                compensation = CirclePointTransaction(
                    persona_id=original.persona_id,
                    game_account_id=original.game_account_id,
                    type=PLACEMENT_ROLLBACK_TRANSACTION_TYPE,
                    amount=-original.amount,
                    reason=f"room_match_placement_rollback:{race.id}:{reason}",
                    source=PLACEMENT_ROLLBACK_TRANSACTION_SOURCE,
                    related_race_result_id=original.related_race_result_id,
                    created_by_discord_user_id=actor,
                    idempotency_key=f"room-match-placement-rollback:{original.id}",
                )
                session.add(compensation)
                placement_compensation_rows.append(compensation)
            compensation_rows.extend(placement_compensation_rows)

            original_rating_events, compensating_rating_events = _reverse_latest_rating_events(
                session,
                race=race,
                settlement_audit=settlement_audit,
            )
            race.status = MatchRaceStatus.VOIDED.value
            session.flush()

            audit = RaceOperationAudit(
                race_id=race.id,
                action=SETTLEMENT_ROLLBACK_ACTION,
                capability=SETTLEMENT_ROLLBACK_CAPABILITY,
                actor_discord_user_id=actor,
                idempotency_key=request_key,
                request_fingerprint=fingerprint,
                before_json={
                    "race_status": MatchRaceStatus.SETTLED.value,
                    "settlement_audit_id": settlement_audit.id,
                    "settlement_transaction_ids": [settlement_by_bet_id[bet.id].id for bet in bets],
                    "placement_transaction_ids": [row.id for row in placement_transactions],
                },
                after_json={
                    "race_status": race.status,
                    "point_compensation_transaction_ids": [row.id for row in compensation_rows],
                    "bet_compensation_transaction_ids": [
                        row.id for row in compensation_rows if row.related_bet_id is not None
                    ],
                    "placement_compensation_transaction_ids": [row.id for row in placement_compensation_rows],
                    "placement_compensations": [
                        {
                            "original_transaction_id": original.id,
                            "compensation_transaction_id": compensation.id,
                            "race_result_id": original.related_race_result_id,
                        }
                        for original, compensation in zip(
                            placement_transactions,
                            placement_compensation_rows,
                            strict=True,
                        )
                    ],
                    "reversed_rating_event_ids": [row.id for row in original_rating_events],
                    "compensating_rating_event_ids": [row.id for row in compensating_rating_events],
                },
                reason=reason,
            )
            session.add(audit)
            session.flush()
            return _rollback_dto(
                audit,
                race=race,
                settlement_audit_id=settlement_audit.id,
                transactions=compensation_rows,
                original_rating_events=original_rating_events,
                compensating_rating_events=compensating_rating_events,
            )
    except IntegrityError:
        with session.begin_nested():
            # Recover concurrent unique-key races with the same global order
            # as the normal mutation path.
            lock_rating_write_gate(session)
            session.scalar(select(Race.id).where(Race.id == race_id).with_for_update())
            existing_audit = _load_audit_by_key(session, request_key, lock=True)
            if existing_audit is None:
                raise
            return _idempotent_result(session, existing_audit, fingerprint=fingerprint)


def _validate_rollback_race(race: Race | None) -> None:
    if race is None or race.race_kind != "room_match":
        raise MatchSettlementConflictError("rollback requires an existing room-match race")
    if race.external_source is not None:
        raise MatchSettlementConflictError("imported historical races are read-only")
    if race.status != MatchRaceStatus.SETTLED.value:
        raise MatchSettlementConflictError("rollback requires a settled room-match race")


def _load_original_settlement_audit(session: Session, *, race_id: int) -> RaceOperationAudit:
    rows = tuple(
        session.scalars(
            select(RaceOperationAudit)
            .where(
                RaceOperationAudit.race_id == race_id,
                RaceOperationAudit.action == SETTLEMENT_CONFIRM_ACTION,
            )
            .order_by(RaceOperationAudit.id)
            .with_for_update()
        )
    )
    if len(rows) != 1:
        raise MatchSettlementConflictError("settled race must have exactly one settlement audit")
    return rows[0]


def _validate_judgements(
    bets: tuple[Bet, ...],
    judgements: tuple[BetJudgement, ...],
) -> dict[int, BetJudgement]:
    bet_ids = [bet.id for bet in bets]
    judgement_ids = [row.bet_id for row in judgements]
    if len(judgement_ids) != len(set(judgement_ids)) or set(judgement_ids) != set(bet_ids):
        raise MatchSettlementConflictError("settled race judgement coverage is inconsistent")
    by_bet_id = {row.bet_id: row for row in judgements}
    for bet in bets:
        judgement = by_bet_id[bet.id]
        if bet.status != "settled":
            raise MatchSettlementConflictError("all settled-race bets must be settled before rollback")
        if judgement.race_id != bet.race_id or judgement.stake_amount != bet.amount:
            raise MatchSettlementConflictError("settled judgement does not match its Bet")
        if judgement.judgement_status not in {"hit", "miss"}:
            raise MatchSettlementConflictError("settled judgement status is invalid")
        if judgement.is_hit is not (judgement.judgement_status == "hit"):
            raise MatchSettlementConflictError("settled judgement hit flag is inconsistent")
    return by_bet_id


def _load_bet_transactions(session: Session, bets: tuple[Bet, ...]) -> tuple[CirclePointTransaction, ...]:
    if not bets:
        return ()
    return tuple(
        session.scalars(
            select(CirclePointTransaction)
            .where(CirclePointTransaction.related_bet_id.in_([bet.id for bet in bets]))
            .order_by(CirclePointTransaction.id)
            .with_for_update()
        )
    )


def _load_original_placement_transactions(
    session: Session,
    *,
    race_id: int,
    settlement_audit: RaceOperationAudit,
    plan: NativePlacementPlan,
) -> tuple[CirclePointTransaction, ...]:
    after = settlement_audit.after_json
    if not isinstance(after, dict):
        raise MatchSettlementConflictError("settlement audit placement provenance is invalid")
    recorded_ids = after.get("placement_transaction_ids")
    if not _integer_list(recorded_ids):
        raise MatchSettlementConflictError("settlement audit placement transaction coverage is invalid")
    if (
        after.get("placement_policy_checksum") != plan.policy_checksum
        or after.get("placement_result_snapshot_checksum") != plan.result_snapshot_checksum
        or after.get("placement_selections") != plan.audit_selections()
    ):
        raise MatchSettlementConflictError("settlement audit placement provenance is inconsistent")

    transactions = (
        tuple(
            session.scalars(
                select(CirclePointTransaction)
                .where(CirclePointTransaction.id.in_(recorded_ids))
                .order_by(CirclePointTransaction.id)
                .with_for_update()
            )
        )
        if recorded_ids
        else ()
    )
    if [row.id for row in transactions] != recorded_ids:
        raise MatchSettlementConflictError("settlement placement transaction set is incomplete")
    by_result_id = {row.related_race_result_id: row for row in transactions}
    if len(by_result_id) != len(transactions):
        raise MatchSettlementConflictError("settlement placement Result coverage is inconsistent")
    for selection in plan.selections:
        transaction = by_result_id.get(selection.result_id)
        if (
            transaction is None
            or transaction.type != PLACEMENT_TRANSACTION_TYPE
            or transaction.source != PLACEMENT_TRANSACTION_SOURCE
            or transaction.related_bet_id is not None
            or transaction.persona_id != selection.owner_at_event_persona_id
            or transaction.game_account_id != selection.game_account_id
            or transaction.amount != selection.amount
        ):
            raise MatchSettlementConflictError("settlement placement transaction provenance is inconsistent")
    if set(by_result_id) != {selection.result_id for selection in plan.selections}:
        raise MatchSettlementConflictError("settlement placement transaction set is inconsistent")

    race_result_ids = tuple(
        session.scalars(select(RaceResult.id).where(RaceResult.race_id == race_id).order_by(RaceResult.id))
    )
    related_rows = (
        tuple(
            session.scalars(
                select(CirclePointTransaction)
                .where(
                    CirclePointTransaction.related_race_result_id.in_(race_result_ids),
                    CirclePointTransaction.type.in_((PLACEMENT_TRANSACTION_TYPE, PLACEMENT_ROLLBACK_TRANSACTION_TYPE)),
                )
                .order_by(CirclePointTransaction.id)
                .with_for_update()
            )
        )
        if race_result_ids
        else ()
    )
    if [row.id for row in related_rows] != recorded_ids:
        raise MatchSettlementConflictError("settlement placement ledger coverage is inconsistent")
    return transactions


def _validate_transaction_history(
    bets: tuple[Bet, ...],
    judgements_by_bet_id: Mapping[int, BetJudgement],
    transactions: tuple[CirclePointTransaction, ...],
    *,
    settlement_audit: RaceOperationAudit,
) -> tuple[dict[int, CirclePointTransaction], dict[int, CirclePointTransaction]]:
    rows_by_bet_id: dict[int, list[CirclePointTransaction]] = defaultdict(list)
    for row in transactions:
        if row.related_bet_id is not None:
            rows_by_bet_id[row.related_bet_id].append(row)

    settlement_by_bet_id: dict[int, CirclePointTransaction] = {}
    stake_by_bet_id: dict[int, CirclePointTransaction] = {}
    for bet in bets:
        rows = rows_by_bet_id.get(bet.id, [])
        settlements = [row for row in rows if row.type == SETTLEMENT_TRANSACTION_TYPE]
        stakes = [row for row in rows if row.type == STAKE_TRANSACTION_TYPE]
        if len(rows) != 2 or len(settlements) != 1 or len(stakes) != 1:
            raise MatchSettlementConflictError("settled Bet transaction history is inconsistent")
        settlement = settlements[0]
        stake = stakes[0]
        judgement = judgements_by_bet_id[bet.id]
        if (
            settlement.game_account_id != bet.game_account_id
            or settlement.persona_id != bet.persona_id
            or settlement.amount != judgement.payout_amount
            or settlement.source != "room_match_settlement"
            or stake.game_account_id != bet.game_account_id
            or stake.persona_id != bet.persona_id
            or stake.amount != -bet.amount
            or stake.source != "room_match_bet"
        ):
            raise MatchSettlementConflictError("settled Bet ledger provenance is inconsistent")
        settlement_by_bet_id[bet.id] = settlement
        stake_by_bet_id[bet.id] = stake

    recorded_ids = settlement_audit.after_json.get("bet_transaction_ids")
    if recorded_ids is None:
        recorded_ids = settlement_audit.after_json.get("settlement_transaction_ids")
    expected_ids = [settlement_by_bet_id[bet.id].id for bet in bets]
    if recorded_ids != expected_ids:
        raise MatchSettlementConflictError("settlement audit transaction coverage is inconsistent")
    return settlement_by_bet_id, stake_by_bet_id


def _reverse_latest_rating_events(
    session: Session,
    *,
    race: Race,
    settlement_audit: RaceOperationAudit,
) -> tuple[tuple[RatingEvent, ...], tuple[RatingEvent, ...]]:
    condition = session.scalar(select(RaceCondition).where(RaceCondition.race_id == race.id).with_for_update())
    if condition is None:
        raise MatchSettlementConflictError("settled race conditions are missing")
    grade = normalize_rating_grade(condition.grade)
    context = session.scalar(select(RaceRatingContext).where(RaceRatingContext.race_id == race.id).with_for_update())
    events = tuple(
        session.scalars(
            select(RatingEvent).where(RatingEvent.race_id == race.id).order_by(RatingEvent.id).with_for_update()
        )
    )
    audit_after = settlement_audit.after_json
    if not isinstance(audit_after, dict):
        raise MatchSettlementConflictError("settlement audit Rating provenance is invalid")
    recorded_event_ids = audit_after.get("rating_event_ids")
    recorded_rule_version_id = audit_after.get("rating_rule_version_id")
    if grade == "OP":
        if context is not None or events or recorded_event_ids != [] or recorded_rule_version_id is not None:
            raise MatchSettlementConflictError("OP settlement has inconsistent Rating history")
        return (), ()
    if context is None or not events:
        raise MatchSettlementConflictError("settled race Rating history is incomplete")
    results = tuple(
        session.scalars(
            select(RaceResult)
            .where(
                RaceResult.race_id == race.id,
                RaceResult.is_rating_excluded.is_(False),
                RaceResult.is_result_void.is_(False),
            )
            .order_by(RaceResult.rank, RaceResult.id)
            .with_for_update()
        )
    )
    _validate_rating_provenance(
        condition=condition,
        context=context,
        results=results,
        events=events,
        grade=grade,
        recorded_event_ids=recorded_event_ids,
        recorded_rule_version_id=recorded_rule_version_id,
    )
    for event in events:
        latest_id = session.scalar(
            select(RatingEvent.id)
            .where(RatingEvent.game_account_id == event.game_account_id)
            .order_by(RatingEvent.id.desc())
            .limit(1)
            .with_for_update()
        )
        if latest_id != event.id:
            raise MatchSettlementConflictError("settlement rollback requires the latest Rating event for every account")

    compensations: list[RatingEvent] = []
    for event in events:
        total_delta = event.rating_before - event.rating_after
        compensation = RatingEvent(
            rating_rule_version_id=event.rating_rule_version_id,
            race_id=event.race_id,
            race_result_id=event.race_result_id,
            game_account_id=event.game_account_id,
            grade=event.grade,
            rank=event.rank,
            converted_rank=event.converted_rank,
            rating_before=event.rating_after,
            base_delta=Decimal(),
            adjustment_delta=total_delta,
            total_delta=total_delta,
            rating_after=event.rating_before,
        )
        session.add(compensation)
        compensations.append(compensation)
    session.flush()
    return events, tuple(compensations)


def _validate_rating_provenance(
    *,
    condition: RaceCondition,
    context: RaceRatingContext,
    results: tuple[RaceResult, ...],
    events: tuple[RatingEvent, ...],
    grade: str,
    recorded_event_ids: object,
    recorded_rule_version_id: object,
) -> None:
    if len(results) < 2 or any(result.game_account_id is None for result in results):
        raise MatchSettlementConflictError("settled race Rating result coverage is inconsistent")
    result_account_ids = [result.game_account_id for result in results]
    if len(result_account_ids) != len(set(result_account_ids)):
        raise MatchSettlementConflictError("settled race has duplicate Rating participants")
    if not _integer_list(recorded_event_ids) or recorded_event_ids != [event.id for event in events]:
        raise MatchSettlementConflictError("settlement audit Rating event coverage is inconsistent")

    rule_version_ids = {event.rating_rule_version_id for event in events}
    if (
        len(rule_version_ids) != 1
        or recorded_rule_version_id not in rule_version_ids
        or isinstance(recorded_rule_version_id, bool)
        or not isinstance(recorded_rule_version_id, int)
    ):
        raise MatchSettlementConflictError("settled race Rating rule version is inconsistent")

    results_by_id = {result.id: result for result in results}
    event_result_ids = [event.race_result_id for event in events]
    if len(event_result_ids) != len(set(event_result_ids)) or set(event_result_ids) != set(results_by_id):
        raise MatchSettlementConflictError("settled race Rating event coverage is inconsistent")
    for event in events:
        result = results_by_id[event.race_result_id]
        if (
            event.race_id != condition.race_id
            or event.game_account_id != result.game_account_id
            or event.rank != result.rank
            or event.converted_rank != result.converted_rank
            or event.grade != grade
            or event.rating_rule_version_id != recorded_rule_version_id
        ):
            raise MatchSettlementConflictError("settled race Rating event provenance is inconsistent")

    context_json = context.raw_context_json
    expected_result_ids = [result.id for result in results]
    expected_account_ids = [result.game_account_id for result in results]
    if (
        context.race_id != condition.race_id
        or context.grade != grade
        or context.participant_count != condition.participant_count
        or context.average_rating_before is None
        or not isinstance(context_json, dict)
        or context_json.get("rating_rule_version_id") != recorded_rule_version_id
        or context_json.get("eligible_result_ids") != expected_result_ids
        or context_json.get("eligible_game_account_ids") != expected_account_ids
        or context_json.get("average_rating_before") != format(context.average_rating_before, "f")
    ):
        raise MatchSettlementConflictError("settled race Rating context provenance is inconsistent")


def _idempotent_result(
    session: Session,
    audit: RaceOperationAudit,
    *,
    fingerprint: str,
) -> MatchSettlementRollbackDTO:
    if audit.action != SETTLEMENT_ROLLBACK_ACTION or audit.request_fingerprint != fingerprint:
        raise MatchSettlementConflictError("idempotency key payload does not match the original rollback")
    race = session.scalar(select(Race).where(Race.id == audit.race_id).with_for_update())
    if race is None or race.status != MatchRaceStatus.VOIDED.value:
        raise MatchSettlementConflictError("stored rollback Race state is inconsistent")
    before = audit.before_json
    after = audit.after_json
    settlement_audit_id = before.get("settlement_audit_id")
    transaction_ids = after.get("point_compensation_transaction_ids")
    original_rating_ids = after.get("reversed_rating_event_ids")
    compensation_rating_ids = after.get("compensating_rating_event_ids")
    placement_compensation_ids = after.get("placement_compensation_transaction_ids")
    if (
        not isinstance(settlement_audit_id, int)
        or not _integer_list(transaction_ids)
        or not _integer_list(original_rating_ids)
        or not _integer_list(compensation_rating_ids)
        or not _integer_list(placement_compensation_ids)
    ):
        raise MatchSettlementConflictError("stored rollback audit payload is invalid")
    transactions = tuple(
        session.scalars(
            select(CirclePointTransaction)
            .where(CirclePointTransaction.id.in_(transaction_ids))
            .order_by(CirclePointTransaction.id)
            .with_for_update()
        )
    )
    original_events = tuple(
        session.scalars(select(RatingEvent).where(RatingEvent.id.in_(original_rating_ids)).order_by(RatingEvent.id))
    )
    compensation_events = tuple(
        session.scalars(select(RatingEvent).where(RatingEvent.id.in_(compensation_rating_ids)).order_by(RatingEvent.id))
    )
    if (
        [row.id for row in transactions] != transaction_ids
        or [row.id for row in original_events] != original_rating_ids
        or [row.id for row in compensation_events] != compensation_rating_ids
        or any(
            (row.type == PLACEMENT_ROLLBACK_TRANSACTION_TYPE and row.source != PLACEMENT_ROLLBACK_TRANSACTION_SOURCE)
            or (row.type != PLACEMENT_ROLLBACK_TRANSACTION_TYPE and row.source != "room_match_settlement_rollback")
            for row in transactions
        )
        or {row.id for row in transactions if row.type == PLACEMENT_ROLLBACK_TRANSACTION_TYPE}
        != set(placement_compensation_ids)
    ):
        raise MatchSettlementConflictError("stored rollback compensation state is inconsistent")
    _validate_idempotent_placement_compensations(
        session,
        race_id=race.id,
        rollback_audit=audit,
        compensation_transactions=transactions,
    )
    return _rollback_dto(
        audit,
        race=race,
        settlement_audit_id=settlement_audit_id,
        transactions=transactions,
        original_rating_events=original_events,
        compensating_rating_events=compensation_events,
    )


def _validate_idempotent_placement_compensations(
    session: Session,
    *,
    race_id: int,
    rollback_audit: RaceOperationAudit,
    compensation_transactions: tuple[CirclePointTransaction, ...],
) -> None:
    before = rollback_audit.before_json
    after = rollback_audit.after_json
    original_ids = before.get("placement_transaction_ids")
    compensation_ids = after.get("placement_compensation_transaction_ids")
    recorded_mapping = after.get("placement_compensations")
    if (
        not _integer_list(original_ids)
        or not _integer_list(compensation_ids)
        or not isinstance(recorded_mapping, list)
        or len(original_ids) != len(compensation_ids)
    ):
        raise MatchSettlementConflictError("stored rollback placement audit payload is invalid")

    originals = (
        tuple(
            session.scalars(
                select(CirclePointTransaction)
                .where(CirclePointTransaction.id.in_(original_ids))
                .order_by(CirclePointTransaction.id)
                .with_for_update()
            )
        )
        if original_ids
        else ()
    )
    compensations_by_id = {row.id: row for row in compensation_transactions}
    compensations = tuple(compensations_by_id.get(row_id) for row_id in compensation_ids)
    if [row.id for row in originals] != original_ids or any(row is None for row in compensations):
        raise MatchSettlementConflictError("stored rollback placement transaction set is incomplete")

    expected_mapping: list[dict[str, int | None]] = []
    for original, compensation in zip(originals, compensations, strict=True):
        assert compensation is not None
        if (
            original.type != PLACEMENT_TRANSACTION_TYPE
            or original.source != PLACEMENT_TRANSACTION_SOURCE
            or original.related_bet_id is not None
            or original.related_race_result_id is None
            or compensation.type != PLACEMENT_ROLLBACK_TRANSACTION_TYPE
            or compensation.source != PLACEMENT_ROLLBACK_TRANSACTION_SOURCE
            or compensation.related_bet_id is not None
            or compensation.related_race_result_id != original.related_race_result_id
            or compensation.persona_id != original.persona_id
            or compensation.game_account_id != original.game_account_id
            or compensation.amount != -original.amount
        ):
            raise MatchSettlementConflictError("stored rollback placement compensation provenance is inconsistent")
        expected_mapping.append(
            {
                "original_transaction_id": original.id,
                "compensation_transaction_id": compensation.id,
                "race_result_id": original.related_race_result_id,
            }
        )
    if recorded_mapping != expected_mapping:
        raise MatchSettlementConflictError("stored rollback placement compensation mapping is inconsistent")

    race_result_ids = tuple(
        session.scalars(select(RaceResult.id).where(RaceResult.race_id == race_id).order_by(RaceResult.id))
    )
    actual_compensation_ids = (
        tuple(
            session.scalars(
                select(CirclePointTransaction.id)
                .where(
                    CirclePointTransaction.related_race_result_id.in_(race_result_ids),
                    CirclePointTransaction.type == PLACEMENT_ROLLBACK_TRANSACTION_TYPE,
                )
                .order_by(CirclePointTransaction.id)
                .with_for_update()
            )
        )
        if race_result_ids
        else ()
    )
    if set(actual_compensation_ids) != set(compensation_ids):
        raise MatchSettlementConflictError("stored rollback placement compensation coverage is inconsistent")


def _rollback_dto(
    audit: RaceOperationAudit,
    *,
    race: Race,
    settlement_audit_id: int,
    transactions: tuple[CirclePointTransaction, ...] | list[CirclePointTransaction],
    original_rating_events: tuple[RatingEvent, ...],
    compensating_rating_events: tuple[RatingEvent, ...],
) -> MatchSettlementRollbackDTO:
    return MatchSettlementRollbackDTO(
        action=SETTLEMENT_ROLLBACK_ACTION,
        audit_id=audit.id,
        race_id=race.id,
        race_status=race.status,
        original_settlement_audit_id=settlement_audit_id,
        transactions=tuple(_transaction_dto(row) for row in transactions),
        reversed_rating_event_ids=tuple(row.id for row in original_rating_events),
        compensating_rating_event_ids=tuple(row.id for row in compensating_rating_events),
    )


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


def _load_audit_by_key(session: Session, key: str, *, lock: bool) -> RaceOperationAudit | None:
    query = select(RaceOperationAudit).where(RaceOperationAudit.idempotency_key == key)
    if lock:
        query = query.with_for_update()
    return session.scalar(query)


def _integer_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, int) and not isinstance(item, bool) for item in value)


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
