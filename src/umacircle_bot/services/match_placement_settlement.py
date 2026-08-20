from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    CirclePointAccount,
    CirclePointTransaction,
    RaceCondition,
    RaceEntry,
    RaceResult,
)
from umacircle_bot.domain.betting import validate_circle_point_balance
from umacircle_bot.domain.errors import (
    BettingRuleError,
    MatchPlacementRewardError,
    MatchSettlementConflictError,
)
from umacircle_bot.domain.match_placement_rewards import (
    MATCH_PLACEMENT_REWARD_TABLE,
    MatchPlacementCandidate,
    allocate_match_placement_rewards,
    placement_reward_policy_basis,
)

PLACEMENT_TRANSACTION_TYPE = "match_placement_reward"
PLACEMENT_TRANSACTION_SOURCE = "room_match_settlement"
PLACEMENT_ROLLBACK_TRANSACTION_TYPE = "match_placement_reward_rollback"
PLACEMENT_ROLLBACK_TRANSACTION_SOURCE = "room_match_terminal_rollback"


@dataclass(frozen=True, slots=True)
class NativePlacementSelection:
    result_id: int
    result_business_key: str
    game_account_id: int
    owner_at_event_persona_id: str
    entry_number: int
    rank: int
    amount: int
    suppressed_result_ids: tuple[int, ...]
    suppressed_result_business_keys: tuple[str, ...]

    def audit_json(self) -> dict[str, object]:
        return {
            "result_id": self.result_id,
            "result_business_key": self.result_business_key,
            "game_account_id": self.game_account_id,
            "owner_at_event_persona_id": self.owner_at_event_persona_id,
            "entry_number": self.entry_number,
            "rank": self.rank,
            "amount": self.amount,
            "suppressed_result_ids": list(self.suppressed_result_ids),
            "suppressed_result_business_keys": list(self.suppressed_result_business_keys),
        }


@dataclass(frozen=True, slots=True)
class NativePlacementPlan:
    race_id: int
    grade: str
    participant_count: int
    policy_checksum: str
    result_snapshot_checksum: str
    selections: tuple[NativePlacementSelection, ...]

    @property
    def persona_ids(self) -> tuple[str, ...]:
        return tuple(sorted({selection.owner_at_event_persona_id for selection in self.selections}))

    def audit_selections(self) -> list[dict[str, object]]:
        return [selection.audit_json() for selection in self.selections]


def build_native_placement_plan(session: Session, *, race_id: int) -> NativePlacementPlan:
    """Validate one native final snapshot and allocate Persona-capped rewards."""

    condition = session.scalar(select(RaceCondition).where(RaceCondition.race_id == race_id).with_for_update())
    if condition is None:
        raise MatchSettlementConflictError("placement settlement requires persisted race conditions")
    entries = tuple(
        session.scalars(
            select(RaceEntry)
            .where(
                RaceEntry.race_id == race_id,
                RaceEntry.entry_kind == "room_match",
            )
            .order_by(RaceEntry.entry_number)
            .with_for_update()
        )
    )
    results = tuple(
        session.scalars(
            select(RaceResult).where(RaceResult.race_id == race_id).order_by(RaceResult.entry_number).with_for_update()
        )
    )
    if (
        condition.participant_count <= 0
        or len(entries) != condition.participant_count
        or len(results) != condition.participant_count
    ):
        raise MatchSettlementConflictError(
            "placement settlement requires final Entry/Result count to match race conditions"
        )

    grade = condition.grade.strip().upper()
    if grade != "OP" and grade not in MATCH_PLACEMENT_REWARD_TABLE:
        raise MatchSettlementConflictError(f"unsupported placement reward grade: {grade}")

    entries_by_number = {entry.entry_number: entry for entry in entries}
    candidates: list[MatchPlacementCandidate] = []
    result_ids_by_key: dict[str, int] = {}
    snapshot_rows: list[dict[str, object]] = []
    for result in results:
        entry = entries_by_number.get(result.entry_number)
        if entry is None or result.is_result_void:
            raise MatchSettlementConflictError("placement settlement requires a complete non-void result snapshot")
        if grade != "OP" and (result.game_account_id is None or result.owner_at_event_persona_id is None):
            raise MatchSettlementConflictError(
                "placement settlement requires Result GameAccount and owner-at-event Persona provenance"
            )
        if (
            entry.game_account_id != result.game_account_id
            or entry.owner_at_event_persona_id != result.owner_at_event_persona_id
        ):
            raise MatchSettlementConflictError("placement settlement Entry/Result ownership provenance is inconsistent")
        if grade != "OP":
            assert result.game_account_id is not None
            assert result.owner_at_event_persona_id is not None
            result_key = _result_business_key(result.id)
            result_ids_by_key[result_key] = result.id
            candidates.append(
                MatchPlacementCandidate(
                    result_business_key=result_key,
                    game_account_business_key=f"game-account:{result.game_account_id}",
                    owner_business_key=result.owner_at_event_persona_id,
                    entry_number=result.entry_number,
                    rank=result.rank,
                )
            )
        snapshot_rows.append(
            {
                "result_id": result.id,
                "entry_number": result.entry_number,
                "rank": result.rank,
                "game_account_id": result.game_account_id,
                "owner_at_event_persona_id": result.owner_at_event_persona_id,
                "is_rating_excluded": result.is_rating_excluded,
                "is_result_void": result.is_result_void,
            }
        )

    policy_basis = placement_reward_policy_basis()
    try:
        allocated = allocate_match_placement_rewards(
            candidates,
            grade=condition.grade,
            participant_count=condition.participant_count,
        )
    except MatchPlacementRewardError as exc:
        raise MatchSettlementConflictError(str(exc)) from exc

    selections = tuple(
        NativePlacementSelection(
            result_id=result_ids_by_key[selection.candidate.result_business_key],
            result_business_key=selection.candidate.result_business_key,
            game_account_id=int(selection.candidate.game_account_business_key.removeprefix("game-account:")),
            owner_at_event_persona_id=selection.candidate.owner_business_key,
            entry_number=selection.candidate.entry_number,
            rank=selection.candidate.rank,
            amount=selection.amount,
            suppressed_result_ids=tuple(result_ids_by_key[key] for key in selection.suppressed_result_business_keys),
            suppressed_result_business_keys=selection.suppressed_result_business_keys,
        )
        for selection in allocated
    )
    return NativePlacementPlan(
        race_id=race_id,
        grade=grade,
        participant_count=condition.participant_count,
        policy_checksum=_checksum(policy_basis),
        result_snapshot_checksum=_checksum(
            {
                "race_id": race_id,
                "grade": grade,
                "participant_count": condition.participant_count,
                "results": snapshot_rows,
            }
        ),
        selections=selections,
    )


def create_native_placement_rewards(
    session: Session,
    *,
    plan: NativePlacementPlan,
    wallets_by_persona: Mapping[str, CirclePointAccount],
    actor_discord_user_id: str,
) -> tuple[CirclePointTransaction, ...]:
    if not plan.selections:
        return ()

    result_ids = [selection.result_id for selection in plan.selections]
    existing = tuple(
        session.scalars(
            select(CirclePointTransaction)
            .where(
                CirclePointTransaction.related_race_result_id.in_(result_ids),
                CirclePointTransaction.type.in_((PLACEMENT_TRANSACTION_TYPE, PLACEMENT_ROLLBACK_TRANSACTION_TYPE)),
            )
            .order_by(CirclePointTransaction.id)
            .with_for_update()
        )
    )
    if existing:
        raise MatchSettlementConflictError("placement transactions already exist for the native Result set")

    deltas: dict[str, int] = defaultdict(int)
    for selection in plan.selections:
        wallet = wallets_by_persona.get(selection.owner_at_event_persona_id)
        if wallet is None or wallet.persona_id != selection.owner_at_event_persona_id:
            raise MatchSettlementConflictError("placement settlement wallet coverage is incomplete")
        deltas[selection.owner_at_event_persona_id] += selection.amount
    for persona_id, delta in deltas.items():
        try:
            validate_circle_point_balance(wallets_by_persona[persona_id].balance + delta)
        except BettingRuleError as exc:
            raise MatchSettlementConflictError(
                "placement settlement would exceed the supported Circle Point balance range"
            ) from exc

    transactions: list[CirclePointTransaction] = []
    for selection in plan.selections:
        wallets_by_persona[selection.owner_at_event_persona_id].balance += selection.amount
        transaction = CirclePointTransaction(
            persona_id=selection.owner_at_event_persona_id,
            game_account_id=selection.game_account_id,
            type=PLACEMENT_TRANSACTION_TYPE,
            amount=selection.amount,
            reason=f"room_match_placement_reward:{plan.race_id}",
            source=PLACEMENT_TRANSACTION_SOURCE,
            related_race_result_id=selection.result_id,
            created_by_discord_user_id=actor_discord_user_id,
            idempotency_key=f"room-match-placement-result:{selection.result_id}",
        )
        session.add(transaction)
        transactions.append(transaction)
    session.flush()
    return tuple(transactions)


def _result_business_key(result_id: int) -> str:
    return f"race-result:{result_id}"


def _checksum(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()
