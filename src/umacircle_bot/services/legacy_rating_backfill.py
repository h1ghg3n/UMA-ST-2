from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    CirclePointAccount,
    CirclePointTransaction,
    Race,
    RaceRatingContext,
    RaceResult,
    RatingEvent,
    RatingRuleVersion,
    SheetImportRun,
)
from umacircle_bot.domain.errors import LegacyImportError, MatchRatingConflictError
from umacircle_bot.domain.imports import normalize_import_source_identifier, normalize_sha256_hex
from umacircle_bot.services.legacy_rating_chain_disposition import (
    load_applied_legacy_rating_chain_dispositions,
)
from umacircle_bot.services.legacy_result_import import LEGACY_RESULT_IMPORT_KIND
from umacircle_bot.services.match_rating import (
    SOURCE_RATING_FIELDS,
    lock_rating_write_gate,
    record_imported_match_rating,
)

SOURCE_RATING_TOLERANCE = Decimal("0.0000001")


@dataclass(frozen=True, slots=True)
class LegacyRatingBackfillResult:
    source_identifier: str
    source_checksum: str
    disposition_decision_checksum: str
    rating_rule_version_id: int
    race_ids: tuple[int, ...]
    rated_race_count: int
    excluded_race_count: int
    event_count: int
    history_context_only_result_count: int
    recalculation_start_race_id: int | None = None
    recalculated_race_count: int = 0


def backfill_imported_match_ratings(
    session: Session,
    *,
    source_identifier: str,
    source_checksum: str,
    confirmed_disposition_checksum: str,
    rating_rule_version_id: int,
) -> LegacyRatingBackfillResult:
    """Build native Rating history from linked imported results without point writes.

    This is a one-shot, pre-operation rebuild step. The caller owns the outer
    transaction and commit. Any incomplete identity/rule coverage rolls back the
    entire backfill.
    """

    normalized_source = normalize_import_source_identifier(source_identifier)
    normalized_checksum = normalize_sha256_hex(source_checksum, field_name="source checksum")
    normalized_disposition_checksum = normalize_sha256_hex(
        confirmed_disposition_checksum,
        field_name="confirmed disposition checksum",
    )
    if (
        not isinstance(rating_rule_version_id, int)
        or isinstance(rating_rule_version_id, bool)
        or rating_rule_version_id <= 0
    ):
        raise LegacyImportError("rating rule version ID must be a positive integer")
    _require_clean_session(session)

    with session.begin_nested():
        lock_rating_write_gate(session)
        version = session.scalar(
            select(RatingRuleVersion).where(RatingRuleVersion.id == rating_rule_version_id).with_for_update()
        )
        if version is None:
            raise LegacyImportError("approved RatingRule version does not exist")
        if version.source_checksum != normalized_checksum:
            raise MatchRatingConflictError("RatingRule version does not match the confirmed source checksum")
        result_runs = tuple(
            session.scalars(
                select(SheetImportRun)
                .where(
                    SheetImportRun.import_kind == LEGACY_RESULT_IMPORT_KIND,
                    SheetImportRun.source_identifier == normalized_source,
                    SheetImportRun.source_checksum == normalized_checksum,
                    SheetImportRun.status == "completed",
                    SheetImportRun.finished_at.is_not(None),
                )
                .with_for_update()
            )
        )
        if len(result_runs) != 1:
            raise LegacyImportError("historical Rating backfill requires one confirmed result import run")
        dispositions = load_applied_legacy_rating_chain_dispositions(
            session,
            source_identifier=normalized_source,
            source_checksum=normalized_checksum,
            confirmed_decision_checksum=normalized_disposition_checksum,
            lock_rows=True,
        )
        if dispositions.unresolved_history_result_ids:
            raise LegacyImportError("historical Rating backfill cannot include unresolved_history results")
        if session.scalar(select(RatingEvent.id).limit(1).with_for_update()) is not None:
            raise MatchRatingConflictError("historical Rating backfill requires an empty RatingEvent ledger")
        if session.scalar(select(RaceRatingContext.id).limit(1).with_for_update()) is not None:
            raise MatchRatingConflictError("historical Rating backfill requires empty Rating contexts")

        races = tuple(
            session.scalars(
                select(Race)
                .where(
                    Race.external_source == normalized_source,
                    Race.race_kind == "room_match",
                )
                .order_by(Race.starts_at, Race.id)
                .with_for_update()
            )
        )
        if not races:
            raise LegacyImportError("historical Rating backfill source has no imported room-match races")
        if any(race.starts_at is None for race in races):
            raise LegacyImportError("historical Rating backfill requires a start time for every imported race")
        if any(race.status not in {"result_confirmed", "settled"} for race in races):
            raise LegacyImportError("historical Rating backfill requires every imported race to be final")
        recalculation_start_race_id = _recalculation_start_race_id(
            session,
            races=races,
            replay_result_ids=dispositions.replay_result_ids,
            trigger_result_ids=dispositions.recalculation_trigger_result_ids,
        )

        points_before = _point_signature(session, lock_rows=True)
        rated_race_count = 0
        excluded_race_count = 0
        event_count = 0
        recalculated_race_count = 0
        recalculate_from_ledger = False
        for race in races:
            if race.id == recalculation_start_race_id:
                recalculate_from_ledger = True
            result = record_imported_match_rating(
                session,
                race_id=race.id,
                rating_rule_version_id=version.id,
                rating_replay_result_ids=_race_disposition_ids(
                    session,
                    race_id=race.id,
                    disposition_result_ids=dispositions.replay_result_ids,
                ),
                history_context_only_result_ids=_race_disposition_ids(
                    session,
                    race_id=race.id,
                    disposition_result_ids=dispositions.history_context_only_result_ids,
                ),
                recalculate_from_ledger=recalculate_from_ledger,
                recalculation_decision_checksum=(dispositions.decision_checksum if recalculate_from_ledger else None),
            )
            if result.rating_rule_version_id is not None:
                verify_source_rating_snapshot(
                    session,
                    race_id=race.id,
                    expected_recalculation_decision_checksum=(
                        dispositions.decision_checksum if recalculate_from_ledger else None
                    ),
                )
                rated_race_count += 1
                event_count += result.event_count
                if recalculate_from_ledger:
                    recalculated_race_count += 1
            else:
                excluded_race_count += 1

        if _point_signature(session, lock_rows=True) != points_before:
            raise MatchRatingConflictError("historical Rating backfill changed Circle Point state")

    return LegacyRatingBackfillResult(
        source_identifier=normalized_source,
        source_checksum=normalized_checksum,
        disposition_decision_checksum=dispositions.decision_checksum,
        rating_rule_version_id=version.id,
        race_ids=tuple(race.id for race in races),
        rated_race_count=rated_race_count,
        excluded_race_count=excluded_race_count,
        event_count=event_count,
        history_context_only_result_count=len(dispositions.history_context_only_result_ids),
        recalculation_start_race_id=recalculation_start_race_id,
        recalculated_race_count=recalculated_race_count,
    )


def _recalculation_start_race_id(
    session: Session,
    *,
    races: tuple[Race, ...],
    replay_result_ids: frozenset[int],
    trigger_result_ids: frozenset[int],
) -> int | None:
    if not trigger_result_ids:
        return None
    if not trigger_result_ids <= replay_result_ids:
        raise LegacyImportError("historical Rating recalculation trigger is not a replay result")
    trigger_rows = tuple(
        session.execute(select(RaceResult.id, RaceResult.race_id).where(RaceResult.id.in_(trigger_result_ids)))
    )
    if {result_id for result_id, _race_id in trigger_rows} != set(trigger_result_ids):
        raise LegacyImportError("historical Rating recalculation trigger coverage changed")
    race_order = {race.id: index for index, race in enumerate(races)}
    trigger_race_ids = {race_id for _result_id, race_id in trigger_rows}
    if not trigger_race_ids <= set(race_order):
        raise LegacyImportError("historical Rating recalculation trigger race changed")
    return min(trigger_race_ids, key=race_order.__getitem__)


def _race_disposition_ids(
    session: Session,
    *,
    race_id: int,
    disposition_result_ids: frozenset[int],
) -> tuple[int, ...]:
    if not disposition_result_ids:
        return ()
    return tuple(
        session.scalars(
            select(RaceResult.id)
            .where(
                RaceResult.race_id == race_id,
                RaceResult.id.in_(disposition_result_ids),
            )
            .order_by(RaceResult.rank, RaceResult.id)
        )
    )


def _point_signature(
    session: Session,
    *,
    lock_rows: bool,
) -> tuple[tuple[tuple[object, ...], ...], tuple[tuple[object, ...], ...]]:
    wallet_statement = select(
        CirclePointAccount.id,
        CirclePointAccount.persona_id,
        CirclePointAccount.balance,
        CirclePointAccount.created_at,
        CirclePointAccount.updated_at,
    ).order_by(CirclePointAccount.id)
    transaction_statement = select(
        CirclePointTransaction.id,
        CirclePointTransaction.persona_id,
        CirclePointTransaction.game_account_id,
        CirclePointTransaction.type,
        CirclePointTransaction.amount,
        CirclePointTransaction.reason,
        CirclePointTransaction.source,
        CirclePointTransaction.related_bet_id,
        CirclePointTransaction.created_by_discord_user_id,
        CirclePointTransaction.idempotency_key,
        CirclePointTransaction.created_at,
    ).order_by(CirclePointTransaction.id)
    if lock_rows:
        wallet_statement = wallet_statement.with_for_update()
        transaction_statement = transaction_statement.with_for_update()
    wallets = tuple(tuple(row) for row in session.execute(wallet_statement))
    transactions = tuple(tuple(row) for row in session.execute(transaction_statement))
    return wallets, transactions


def verify_source_rating_snapshot(
    session: Session,
    *,
    race_id: int,
    expected_recalculation_decision_checksum: str | None = None,
) -> None:
    """Verify immutable source evidence and exact/recalculated native Rating values."""

    context = session.scalar(select(RaceRatingContext).where(RaceRatingContext.race_id == race_id))
    if context is None or context.average_rating_before is None:
        raise MatchRatingConflictError("historical Rating backfill did not create a Rating context")
    raw_context = context.raw_context_json
    if not isinstance(raw_context, dict):
        raise MatchRatingConflictError("historical Rating context evidence is missing")
    mode = raw_context.get("mode")
    if expected_recalculation_decision_checksum is None:
        if mode != "historical_import" or raw_context.get("recalculation_decision_checksum") is not None:
            raise MatchRatingConflictError("historical Rating exact-source context evidence changed")
        is_recalculation = False
    else:
        expected_checksum = normalize_sha256_hex(
            expected_recalculation_decision_checksum,
            field_name="expected recalculation decision checksum",
        )
        if (
            mode != "historical_recalculation"
            or raw_context.get("recalculation_decision_checksum") != expected_checksum
        ):
            raise MatchRatingConflictError("historical Rating recalculation decision evidence changed")
        is_recalculation = True
    eligible_results = tuple(
        session.scalars(
            select(RaceResult)
            .where(
                RaceResult.race_id == race_id,
                RaceResult.is_rating_excluded.is_(False),
                RaceResult.is_result_void.is_(False),
            )
            .order_by(RaceResult.rank, RaceResult.id)
        )
    )
    eligible_ids = tuple(result.id for result in eligible_results)
    replay_ids = _context_result_ids(raw_context, field="rating_replay_result_ids")
    context_only_ids = _context_result_ids(raw_context, field="history_context_only_result_ids")
    if (
        set(replay_ids) & set(context_only_ids)
        or set(replay_ids) | set(context_only_ids) != set(eligible_ids)
        or raw_context.get("eligible_result_ids") != list(eligible_ids)
    ):
        raise MatchRatingConflictError("historical Rating context disposition coverage changed")

    pairs = tuple(
        session.execute(
            select(RatingEvent, RaceResult)
            .join(RaceResult, RaceResult.id == RatingEvent.race_result_id)
            .where(RatingEvent.race_id == race_id)
            .order_by(RatingEvent.rank, RatingEvent.id)
        )
    )
    events_by_result = {event.race_result_id: event for event, _result in pairs}
    if len(events_by_result) != len(pairs) or set(events_by_result) != set(replay_ids):
        raise MatchRatingConflictError("historical Rating event disposition coverage changed")
    if any(
        event.race_id != result.race_id or event.game_account_id != result.game_account_id for event, result in pairs
    ):
        raise MatchRatingConflictError("historical Rating event owner provenance changed")

    calculations = raw_context.get("participant_calculations")
    if not isinstance(calculations, list):
        raise MatchRatingConflictError("historical Rating participant evidence is missing")
    calculation_by_result: dict[int, dict[str, object]] = {}
    for calculation in calculations:
        if not isinstance(calculation, dict):
            raise MatchRatingConflictError("historical Rating participant evidence is malformed")
        result_id = calculation.get("race_result_id")
        if not isinstance(result_id, int) or isinstance(result_id, bool) or result_id in calculation_by_result:
            raise MatchRatingConflictError("historical Rating participant evidence is malformed")
        calculation_by_result[result_id] = calculation
    if set(calculation_by_result) != set(eligible_ids):
        raise MatchRatingConflictError("historical Rating participant evidence coverage changed")

    snapshots: dict[int, dict[str, Decimal]] = {}
    for result in eligible_results:
        raw = result.raw_result_json
        snapshot = raw.get("legacy_rating") if isinstance(raw, dict) else None
        if not isinstance(snapshot, dict):
            raise MatchRatingConflictError("historical Rating source snapshot is missing")
        snapshots[result.id] = {field: _source_decimal(snapshot, field=field) for field in SOURCE_RATING_FIELDS}
    if raw_context.get("source_snapshot_checksum") != _source_snapshot_checksum(snapshots):
        raise MatchRatingConflictError("historical Rating source snapshot checksum changed")

    calculated_by_result: dict[int, dict[str, Decimal]] = {}
    for result in eligible_results:
        source_snapshot = snapshots[result.id]
        evidence = calculation_by_result[result.id]
        expected_disposition = "rating_replay" if result.id in replay_ids else "history_context_only"
        expected_input = (
            "ledger"
            if is_recalculation and result.id in replay_ids
            else "source_context"
            if result.id in context_only_ids
            else "source_exact"
        )
        if (
            evidence.get("disposition") != expected_disposition
            or evidence.get("game_account_id") != result.game_account_id
            or evidence.get("rating_input") != expected_input
            or evidence.get("rank") != result.rank
            or evidence.get("converted_rank") != result.converted_rank
        ):
            raise MatchRatingConflictError("historical Rating participant disposition evidence changed")
        evidence_source = evidence.get("source")
        evidence_calculated = evidence.get("calculated")
        if not isinstance(evidence_source, dict) or not isinstance(evidence_calculated, dict):
            raise MatchRatingConflictError("historical Rating participant calculation evidence is missing")
        calculated = {field: _source_decimal(evidence_calculated, field=field) for field in SOURCE_RATING_FIELDS}
        calculated_by_result[result.id] = calculated
        for field, expected_value in source_snapshot.items():
            source_value = _source_decimal(evidence_source, field=field)
            if abs(source_value - expected_value) > SOURCE_RATING_TOLERANCE:
                raise MatchRatingConflictError(
                    f"historical Rating source mismatch for race {race_id}, result {result.id}, field {field}"
                )
            if not is_recalculation and abs(calculated[field] - expected_value) > SOURCE_RATING_TOLERANCE:
                raise MatchRatingConflictError(
                    f"historical Rating source mismatch for race {race_id}, result {result.id}, field {field}"
                )
        if (
            is_recalculation
            and result.id in context_only_ids
            and abs(calculated["rating_before"] - source_snapshot["rating_before"]) > SOURCE_RATING_TOLERANCE
        ):
            raise MatchRatingConflictError("historical Rating context-only input changed")

    average_before = sum(
        (calculated["rating_before"] for calculated in calculated_by_result.values()),
        Decimal(),
    ) / Decimal(len(calculated_by_result))
    if (
        abs(context.average_rating_before - average_before) > SOURCE_RATING_TOLERANCE
        or abs(_source_decimal(raw_context, field="average_rating_before") - average_before) > SOURCE_RATING_TOLERANCE
    ):
        raise MatchRatingConflictError("historical Rating calculated average changed")

    for result in eligible_results:
        calculated = calculated_by_result[result.id]
        expected_peer_difference = average_before - calculated["rating_before"]
        expected_rating_after = max(
            calculated["rating_before"] + calculated["base_delta"] + calculated["adjustment_delta"],
            Decimal(),
        )
        if (
            abs(calculated["peer_rating_difference"] - expected_peer_difference) > SOURCE_RATING_TOLERANCE
            or abs(calculated["rating_after"] - expected_rating_after) > SOURCE_RATING_TOLERANCE
        ):
            raise MatchRatingConflictError(
                f"historical Rating calculated evidence changed for race {race_id}, result {result.id}"
            )
        event = events_by_result.get(result.id)
        if event is None:
            continue
        actual = {
            "rating_before": event.rating_before,
            "peer_rating_difference": context.average_rating_before - event.rating_before,
            "base_delta": event.base_delta,
            "adjustment_delta": event.adjustment_delta,
            "rating_after": event.rating_after,
        }
        if abs(event.total_delta - (event.base_delta + event.adjustment_delta)) > SOURCE_RATING_TOLERANCE:
            raise MatchRatingConflictError("historical Rating event total changed")
        for field, expected_value in calculated.items():
            if abs(actual[field] - expected_value) > SOURCE_RATING_TOLERANCE:
                raise MatchRatingConflictError(
                    f"historical Rating event mismatch for race {race_id}, result {result.id}, field {field}"
                )


def _context_result_ids(context: dict[str, object], *, field: str) -> tuple[int, ...]:
    values = context.get(field)
    if not isinstance(values, list) or any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in values
    ):
        raise MatchRatingConflictError(f"historical Rating context {field} is malformed")
    if len(values) != len(set(values)):
        raise MatchRatingConflictError(f"historical Rating context {field} is duplicated")
    return tuple(values)


def _source_snapshot_checksum(snapshots: dict[int, dict[str, Decimal]]) -> str:
    payload = [
        {
            "race_result_id": result_id,
            **{field: format(snapshot[field], "f") for field in SOURCE_RATING_FIELDS},
        }
        for result_id, snapshot in sorted(snapshots.items())
    ]
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _source_decimal(snapshot: dict[str, object], *, field: str) -> Decimal:
    value = snapshot.get(field)
    if value is None or isinstance(value, bool):
        raise MatchRatingConflictError(f"historical Rating source field is missing: {field}")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise MatchRatingConflictError(f"historical Rating source field is invalid: {field}") from None


def _require_clean_session(session: Session) -> None:
    if session.new or session.dirty or session.deleted:
        raise LegacyImportError("historical Rating backfill requires a clean session")
