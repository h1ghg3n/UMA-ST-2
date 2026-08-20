from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, InvalidOperation, localcontext
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    RATING_NUMERIC_SCALE,
    Race,
    RaceCondition,
    RaceRatingContext,
    RaceResult,
    RatingEvent,
    RatingRule,
    RatingRuleVersion,
)
from umacircle_bot.domain.errors import MatchRatingConflictError, MatchRatingError

RATING_STORAGE_QUANTUM = Decimal(1).scaleb(-RATING_NUMERIC_SCALE)
RATING_GRADE_FACTORS = {"GI": Decimal("7"), "GII": Decimal("5"), "GIII": Decimal("3")}
GRADE_ALIASES = {"G1": "GI", "G2": "GII", "G3": "GIII"}
SOURCE_RATING_TOLERANCE = Decimal("0.0000001")
SOURCE_RATING_FIELDS = (
    "rating_before",
    "peer_rating_difference",
    "base_delta",
    "adjustment_delta",
    "rating_after",
)


@dataclass(frozen=True)
class MatchRatingResult:
    race_id: int
    rating_rule_version_id: int | None
    grade: str
    participant_count: int
    event_count: int
    average_rating_before: Decimal | None


def lock_rating_write_gate(session: Session) -> None:
    """Serialize every Rating writer on the immutable rule-version catalog."""

    tuple(session.scalars(select(RatingRuleVersion.id).order_by(RatingRuleVersion.id).with_for_update()))


def record_match_rating(
    session: Session,
    *,
    race_id: int,
    rating_rule_version_id: int | None = None,
) -> MatchRatingResult:
    """Record one race's Rating ledger entries without committing the session.

    The caller owns the surrounding transaction.  This function intentionally
    performs no settlement, publication, or Discord interaction.
    """

    lock_rating_write_gate(session)
    race = session.scalar(select(Race).where(Race.id == race_id).with_for_update())
    if race is None or race.race_kind != "room_match":
        raise MatchRatingError("Rating requires an existing room-match race")
    condition = session.scalar(select(RaceCondition).where(RaceCondition.race_id == race.id).with_for_update())
    if condition is None:
        raise MatchRatingError("Rating requires persisted race conditions")
    grade = normalize_rating_grade(condition.grade)
    if grade == "OP":
        return MatchRatingResult(
            race_id=race.id,
            rating_rule_version_id=None,
            grade=grade,
            participant_count=condition.participant_count,
            event_count=0,
            average_rating_before=None,
        )
    if (
        session.scalar(select(RatingEvent.id).where(RatingEvent.race_id == race.id).limit(1).with_for_update())
        is not None
    ):
        raise MatchRatingConflictError("Rating events already exist for this race")
    if (
        session.scalar(
            select(RaceRatingContext.id).where(RaceRatingContext.race_id == race.id).limit(1).with_for_update()
        )
        is not None
    ):
        raise MatchRatingConflictError("a Rating context already exists for this race")
    results = list(
        session.scalars(
            select(RaceResult)
            .where(RaceResult.race_id == race.id)
            .order_by(RaceResult.rank, RaceResult.id)
            .with_for_update()
        )
    )
    eligible = [result for result in results if not result.is_rating_excluded and not result.is_result_void]
    if len(eligible) < 2:
        raise MatchRatingError("Rating requires at least two non-excluded results")
    if condition.participant_count < 2:
        raise MatchRatingError("Rating requires a declared participant count of at least two")
    if any(result.game_account_id is None for result in eligible):
        raise MatchRatingError("every Rating-eligible result requires a linked game account")
    account_ids = [result.game_account_id for result in eligible]
    if len(account_ids) != len(set(account_ids)):
        raise MatchRatingError("a game account may have only one Rating-eligible result per race")

    version = _load_rule_version(session, requested_id=rating_rule_version_id)
    ratings_before = _load_ratings_before(session, account_ids=account_ids)
    average_before = sum(ratings_before.values(), Decimal()) / Decimal(len(eligible))
    converted_ranks = _converted_ranks(eligible, declared_participant_count=condition.participant_count)
    rule_by_rank = _load_rules(
        session,
        version_id=version.id,
        grade=grade,
        participant_count=condition.participant_count,
        converted_ranks=set(converted_ranks.values()),
    )

    events: list[RatingEvent] = []
    for result in eligible:
        assert result.game_account_id is not None
        converted_rank = converted_ranks[result.id]
        if result.converted_rank is not None and result.converted_rank != converted_rank:
            raise MatchRatingConflictError("persisted converted rank conflicts with Rating calculation")
        result.converted_rank = converted_rank
        rating_before = ratings_before[result.game_account_id]
        if grade == "L":
            base_delta = Decimal()
            adjustment_delta = Decimal()
        else:
            base_delta = rule_by_rank[converted_rank]
            adjustment_delta = _adjustment_delta(
                average_rating_before=average_before,
                rating_before=rating_before,
                grade=grade,
            )
        total_delta = base_delta + adjustment_delta
        rating_after = max(rating_before + total_delta, Decimal())
        events.append(
            RatingEvent(
                rating_rule_version_id=version.id,
                race_id=race.id,
                race_result_id=result.id,
                game_account_id=result.game_account_id,
                grade=grade,
                rank=result.rank,
                converted_rank=converted_rank,
                rating_before=_storage_value(rating_before),
                base_delta=_storage_value(base_delta),
                adjustment_delta=_storage_value(adjustment_delta),
                total_delta=_storage_value(total_delta),
                rating_after=_storage_value(rating_after),
            )
        )
    session.add(
        RaceRatingContext(
            race_id=race.id,
            average_rating_before=_storage_value(average_before),
            participant_count=condition.participant_count,
            grade=grade,
            raw_context_json={
                "rating_rule_version_id": version.id,
                "eligible_result_ids": [result.id for result in eligible],
                "eligible_game_account_ids": account_ids,
                "average_rating_before": format(_storage_value(average_before), "f"),
            },
        )
    )
    session.add_all(events)
    session.flush()
    return MatchRatingResult(
        race_id=race.id,
        rating_rule_version_id=version.id,
        grade=grade,
        participant_count=condition.participant_count,
        event_count=len(events),
        average_rating_before=_storage_value(average_before),
    )


def record_imported_match_rating(
    session: Session,
    *,
    race_id: int,
    rating_rule_version_id: int,
    rating_replay_result_ids: Collection[int],
    history_context_only_result_ids: Collection[int],
    recalculate_from_ledger: bool = False,
    recalculation_decision_checksum: str | None = None,
) -> MatchRatingResult:
    """Record one imported race using reviewed historical dispositions.

    The caller owns the surrounding transaction. Source-only participants are
    retained in every calculation and in the context evidence, but do not
    receive RatingEvent rows.
    """

    if not isinstance(recalculate_from_ledger, bool):
        raise MatchRatingError("historical Rating recalculation mode must be boolean")
    normalized_recalculation_checksum = _recalculation_checksum(
        recalculation_decision_checksum,
        required=recalculate_from_ledger,
    )
    lock_rating_write_gate(session)
    race = session.scalar(select(Race).where(Race.id == race_id).with_for_update())
    if race is None or race.race_kind != "room_match":
        raise MatchRatingError("Rating requires an existing room-match race")
    if race.external_source is None:
        raise MatchRatingError("imported Rating requires a historical source race")
    condition = session.scalar(select(RaceCondition).where(RaceCondition.race_id == race.id).with_for_update())
    if condition is None:
        raise MatchRatingError("Rating requires persisted race conditions")
    grade = normalize_rating_grade(condition.grade)
    if grade == "OP":
        return MatchRatingResult(
            race_id=race.id,
            rating_rule_version_id=None,
            grade=grade,
            participant_count=condition.participant_count,
            event_count=0,
            average_rating_before=None,
        )
    if (
        session.scalar(select(RatingEvent.id).where(RatingEvent.race_id == race.id).limit(1).with_for_update())
        is not None
    ):
        raise MatchRatingConflictError("Rating events already exist for this race")
    if (
        session.scalar(
            select(RaceRatingContext.id).where(RaceRatingContext.race_id == race.id).limit(1).with_for_update()
        )
        is not None
    ):
        raise MatchRatingConflictError("a Rating context already exists for this race")

    results = list(
        session.scalars(
            select(RaceResult)
            .where(RaceResult.race_id == race.id)
            .order_by(RaceResult.rank, RaceResult.id)
            .with_for_update()
        )
    )
    eligible = [result for result in results if not result.is_rating_excluded and not result.is_result_void]
    if len(eligible) < 2:
        raise MatchRatingError("Rating requires at least two non-excluded results")
    if condition.participant_count < 2:
        raise MatchRatingError("Rating requires a declared participant count of at least two")

    replay_ids = _historical_result_id_set(rating_replay_result_ids, field="rating replay")
    context_only_ids = _historical_result_id_set(
        history_context_only_result_ids,
        field="history context-only",
    )
    if replay_ids & context_only_ids:
        raise MatchRatingError("historical Rating dispositions must be disjoint")
    eligible_ids = {result.id for result in eligible}
    if replay_ids | context_only_ids != eligible_ids:
        raise MatchRatingError("historical Rating dispositions must exactly cover eligible results")

    replay_results = [result for result in eligible if result.id in replay_ids]
    context_only_results = [result for result in eligible if result.id in context_only_ids]
    if any(result.game_account_id is None for result in replay_results):
        raise MatchRatingError("every rating_replay result requires a linked game account")
    if any(result.game_account_id is not None for result in context_only_results):
        raise MatchRatingError("every history_context_only result requires a null game account")
    account_ids = [result.game_account_id for result in replay_results if result.game_account_id is not None]
    if len(account_ids) != len(set(account_ids)):
        raise MatchRatingError("a game account may have only one Rating-eligible result per race")

    version = _load_rule_version(session, requested_id=rating_rule_version_id)
    snapshots = {result.id: _historical_rating_snapshot(result) for result in eligible}
    source_ratings_before = {result_id: values["rating_before"] for result_id, values in snapshots.items()}
    converted_ranks = _converted_ranks(eligible, declared_participant_count=condition.participant_count)
    rule_by_rank = _load_rules(
        session,
        version_id=version.id,
        grade=grade,
        participant_count=condition.participant_count,
        converted_ranks=set(converted_ranks.values()),
    )
    ledger_ratings = _load_ratings_before(session, account_ids=account_ids)
    ratings_before = dict(source_ratings_before)
    for result in replay_results:
        assert result.game_account_id is not None
        ledger_rating = _storage_value(ledger_ratings[result.game_account_id])
        if not recalculate_from_ledger:
            source_rating = _storage_value(source_ratings_before[result.id])
            if abs(ledger_rating - source_rating) > SOURCE_RATING_TOLERANCE:
                raise MatchRatingConflictError(
                    f"historical Rating ledger does not match source rating_before for result {result.id}"
                )
        ratings_before[result.id] = ledger_rating
    average_before = sum(ratings_before.values(), Decimal()) / Decimal(len(eligible))

    events: list[RatingEvent] = []
    participant_calculations: list[dict[str, object]] = []
    for result in eligible:
        snapshot = snapshots[result.id]
        rating_before = ratings_before[result.id]
        converted_rank = converted_ranks[result.id]
        if result.converted_rank is not None and result.converted_rank != converted_rank:
            raise MatchRatingConflictError("persisted converted rank conflicts with Rating calculation")
        result.converted_rank = converted_rank
        if grade == "L":
            base_delta = Decimal()
            adjustment_delta = Decimal()
        else:
            base_delta = rule_by_rank[converted_rank]
            adjustment_delta = _adjustment_delta(
                average_rating_before=average_before,
                rating_before=rating_before,
                grade=grade,
            )
        rating_after = max(rating_before + base_delta + adjustment_delta, Decimal())
        calculated = {
            "rating_before": rating_before,
            "peer_rating_difference": average_before - rating_before,
            "base_delta": base_delta,
            "adjustment_delta": adjustment_delta,
            "rating_after": rating_after,
        }
        if not recalculate_from_ledger:
            _verify_historical_snapshot(result_id=result.id, source=snapshot, calculated=calculated)

        disposition = "rating_replay" if result.id in replay_ids else "history_context_only"
        participant_calculations.append(
            {
                "race_result_id": result.id,
                "game_account_id": result.game_account_id,
                "disposition": disposition,
                "rating_input": (
                    "ledger"
                    if recalculate_from_ledger and result.id in replay_ids
                    else "source_context"
                    if result.id in context_only_ids
                    else "source_exact"
                ),
                "rank": result.rank,
                "converted_rank": converted_rank,
                "source": {field: format(snapshot[field], "f") for field in SOURCE_RATING_FIELDS},
                "calculated": {field: format(_storage_value(calculated[field]), "f") for field in SOURCE_RATING_FIELDS},
            }
        )
        if result.id in context_only_ids:
            continue
        assert result.game_account_id is not None
        total_delta = base_delta + adjustment_delta
        events.append(
            RatingEvent(
                rating_rule_version_id=version.id,
                race_id=race.id,
                race_result_id=result.id,
                game_account_id=result.game_account_id,
                grade=grade,
                rank=result.rank,
                converted_rank=converted_rank,
                rating_before=_storage_value(rating_before),
                base_delta=_storage_value(base_delta),
                adjustment_delta=_storage_value(adjustment_delta),
                total_delta=_storage_value(total_delta),
                rating_after=_storage_value(rating_after),
            )
        )

    raw_context: dict[str, object] = {
        "mode": "historical_recalculation" if recalculate_from_ledger else "historical_import",
        "rating_rule_version_id": version.id,
        "rating_replay_result_ids": sorted(replay_ids),
        "history_context_only_result_ids": sorted(context_only_ids),
        "eligible_result_ids": [result.id for result in eligible],
        "average_rating_before": format(_storage_value(average_before), "f"),
        "participant_calculations": participant_calculations,
        "source_snapshot_checksum": _historical_snapshot_checksum(snapshots),
    }
    if normalized_recalculation_checksum is not None:
        raw_context["recalculation_decision_checksum"] = normalized_recalculation_checksum
    session.add(
        RaceRatingContext(
            race_id=race.id,
            average_rating_before=_storage_value(average_before),
            participant_count=condition.participant_count,
            grade=grade,
            raw_context_json=raw_context,
        )
    )
    session.add_all(events)
    session.flush()
    return MatchRatingResult(
        race_id=race.id,
        rating_rule_version_id=version.id,
        grade=grade,
        participant_count=condition.participant_count,
        event_count=len(events),
        average_rating_before=_storage_value(average_before),
    )


def _historical_result_id_set(values: Collection[int], *, field: str) -> frozenset[int]:
    if isinstance(values, (str, bytes)):
        raise MatchRatingError(f"{field} result IDs must be a collection of integers")
    items = tuple(values)
    if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in items):
        raise MatchRatingError(f"{field} result IDs must be positive integers")
    result_ids = frozenset(items)
    if len(result_ids) != len(items):
        raise MatchRatingError(f"{field} result IDs must not contain duplicates")
    return result_ids


def _recalculation_checksum(value: str | None, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise MatchRatingError("historical Rating recalculation requires a decision checksum")
        return None
    if not required:
        raise MatchRatingError("historical Rating recalculation checksum requires recalculation mode")
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise MatchRatingError("historical Rating recalculation decision checksum is invalid")
    return normalized


def _historical_rating_snapshot(result: RaceResult) -> dict[str, Decimal]:
    raw = result.raw_result_json
    snapshot = raw.get("legacy_rating") if isinstance(raw, dict) else None
    if not isinstance(snapshot, dict):
        raise MatchRatingConflictError(f"historical Rating source snapshot is missing for result {result.id}")
    return {
        field: _historical_source_decimal(snapshot, field=field, result_id=result.id) for field in SOURCE_RATING_FIELDS
    }


def _historical_source_decimal(snapshot: dict[str, object], *, field: str, result_id: int) -> Decimal:
    value = snapshot.get(field)
    if value is None or isinstance(value, bool):
        raise MatchRatingConflictError(f"historical Rating source field is missing for result {result_id}: {field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise MatchRatingConflictError(
            f"historical Rating source field is invalid for result {result_id}: {field}"
        ) from None
    if not parsed.is_finite():
        raise MatchRatingConflictError(f"historical Rating source field is invalid for result {result_id}: {field}")
    return parsed


def _verify_historical_snapshot(
    *,
    result_id: int,
    source: dict[str, Decimal],
    calculated: dict[str, Decimal],
) -> None:
    for field in SOURCE_RATING_FIELDS:
        if abs(source[field] - calculated[field]) > SOURCE_RATING_TOLERANCE:
            raise MatchRatingConflictError(f"historical Rating source mismatch for result {result_id}, field {field}")


def _historical_snapshot_checksum(snapshots: dict[int, dict[str, Decimal]]) -> str:
    payload = [
        {
            "race_result_id": result_id,
            **{field: format(snapshot[field], "f") for field in SOURCE_RATING_FIELDS},
        }
        for result_id, snapshot in sorted(snapshots.items())
    ]
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return sha256(canonical.encode("utf-8")).hexdigest()


def normalize_rating_grade(value: str) -> str:
    if not isinstance(value, str):
        raise MatchRatingError("race grade must be text")
    normalized = value.strip().upper()
    normalized = GRADE_ALIASES.get(normalized, normalized)
    if normalized not in {*RATING_GRADE_FACTORS, "L", "OP"}:
        raise MatchRatingError("unsupported Rating grade")
    return normalized


def _load_rule_version(session: Session, *, requested_id: int | None) -> RatingRuleVersion:
    if requested_id is not None:
        version = session.scalar(
            select(RatingRuleVersion).where(RatingRuleVersion.id == requested_id).with_for_update()
        )
    else:
        version = session.scalar(
            select(RatingRuleVersion).order_by(RatingRuleVersion.version_number.desc()).limit(1).with_for_update()
        )
    if version is None:
        raise MatchRatingError("Rating requires a seeded RatingRule version")
    return version


def _load_ratings_before(session: Session, *, account_ids: list[int]) -> dict[int, Decimal]:
    ratings = {account_id: Decimal() for account_id in account_ids}
    remaining = set(account_ids)
    events = session.scalars(
        select(RatingEvent)
        .where(RatingEvent.game_account_id.in_(account_ids))
        .order_by(RatingEvent.game_account_id, RatingEvent.id.desc())
    )
    for event in events:
        if event.game_account_id in remaining:
            ratings[event.game_account_id] = event.rating_after
            remaining.remove(event.game_account_id)
    return ratings


def _converted_ranks(results: list[RaceResult], *, declared_participant_count: int) -> dict[int, int]:
    field_size = len(results)
    converted: dict[int, int] = {}
    for ordinal, result in enumerate(results):
        value = ((Decimal(declared_participant_count - 1) * Decimal(ordinal)) / Decimal(field_size - 1)) + 1
        converted[result.id] = int(value.to_integral_value(rounding=ROUND_FLOOR))
    return converted


def _load_rules(
    session: Session,
    *,
    version_id: int,
    grade: str,
    participant_count: int,
    converted_ranks: set[int],
) -> dict[int, Decimal]:
    if grade == "L":
        return {}
    rules = list(
        session.scalars(
            select(RatingRule).where(
                RatingRule.rating_rule_version_id == version_id,
                RatingRule.grade == grade,
                RatingRule.participant_count == participant_count,
                RatingRule.converted_rank.in_(converted_ranks),
            )
        )
    )
    by_rank = {rule.converted_rank: rule.base_delta for rule in rules}
    missing = sorted(converted_ranks - set(by_rank))
    if missing:
        raise MatchRatingError(f"RatingRule version does not cover converted ranks: {missing}")
    return by_rank


def _adjustment_delta(*, average_rating_before: Decimal, rating_before: Decimal, grade: str) -> Decimal:
    difference = average_rating_before - rating_before
    with localcontext() as context:
        context.prec = 60
        if difference >= 0:
            curve = Decimal("1.5") * ((difference + Decimal("10")).log10() - Decimal(1))
        else:
            curve = -((-difference + Decimal("10")).log10()) + Decimal(1)
        return curve * RATING_GRADE_FACTORS[grade]


def _storage_value(value: Decimal) -> Decimal:
    return value.quantize(RATING_STORAGE_QUANTUM)
