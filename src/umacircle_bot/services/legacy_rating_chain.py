from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import Race, RaceCondition, RaceEntry, RaceResult, SheetImportRecord
from umacircle_bot.domain.errors import LegacyImportError
from umacircle_bot.domain.player_link import normalize_player_name_strict

RATING_SNAPSHOT_FIELDS = (
    "rating_before",
    "peer_rating_difference",
    "base_delta",
    "adjustment_delta",
    "rating_after",
)
RATING_REPLAY_SOURCE_EXACT = "source_exact"
RATING_REPLAY_RECALCULATE = "reviewed_recalculation"
RATING_REPLAY_MODES = frozenset(
    {
        RATING_REPLAY_SOURCE_EXACT,
        RATING_REPLAY_RECALCULATE,
    }
)


@dataclass(frozen=True, slots=True)
class LegacyRatingChainRow:
    normalized_player_name: str
    race_id: int
    result_id: int
    starts_at: datetime | None
    source_record_key: str
    snapshot: dict[str, Decimal]
    issues: tuple[str, ...]


def build_legacy_rating_chain_evidence(
    session: Session,
    *,
    source_identifier: str,
) -> dict[str, dict[str, object]]:
    evidence: dict[str, dict[str, object]] = {}
    for normalized_name, rows in load_legacy_rating_chain_rows_by_name(
        session,
        source_identifier=source_identifier,
    ).items():
        issues = legacy_rating_chain_issues(rows)
        evidence[normalized_name] = {
            "status": "complete" if not issues else "broken",
            "rating_result_count": len(rows),
            "initial_rating": _rating_value(rows[0], "rating_before") if rows else None,
            "final_rating": _rating_value(rows[-1], "rating_after") if rows else None,
            "issues": list(issues),
            "rows": [
                {
                    "race_id": row.race_id,
                    "result_id": row.result_id,
                    "starts_at": row.starts_at.isoformat() if row.starts_at is not None else None,
                    "source_record_key": row.source_record_key,
                    "snapshot": {
                        field: format(row.snapshot[field], "f") if field in row.snapshot else None
                        for field in RATING_SNAPSHOT_FIELDS
                    },
                    "issues": list(row.issues),
                }
                for row in rows
            ],
        }
    return evidence


def empty_legacy_rating_chain_evidence() -> dict[str, object]:
    return {
        "status": "not_applicable",
        "rating_result_count": 0,
        "initial_rating": None,
        "final_rating": None,
        "issues": [],
        "rows": [],
    }


def load_legacy_rating_chain_rows_by_name(
    session: Session,
    *,
    source_identifier: str,
) -> dict[str, tuple[LegacyRatingChainRow, ...]]:
    source_rows = tuple(
        session.execute(
            select(
                Race.starts_at,
                Race.id,
                RaceEntry.player_name,
                RaceResult.id,
                RaceResult.raw_result_json,
                SheetImportRecord.source_key,
            )
            .select_from(RaceEntry)
            .join(Race, Race.id == RaceEntry.race_id)
            .join(
                RaceResult,
                and_(
                    RaceResult.race_id == RaceEntry.race_id,
                    RaceResult.entry_number == RaceEntry.entry_number,
                ),
            )
            .join(RaceCondition, RaceCondition.race_id == Race.id)
            .join(SheetImportRecord, SheetImportRecord.id == RaceResult.source_import_record_id)
            .where(
                Race.external_source == source_identifier,
                Race.race_kind == "room_match",
                func.upper(RaceCondition.grade) != "OP",
                RaceResult.is_rating_excluded.is_(False),
                RaceResult.is_result_void.is_(False),
            )
            .order_by(Race.starts_at, Race.id, SheetImportRecord.source_key)
        )
    )
    rows_by_name: dict[str, list[LegacyRatingChainRow]] = defaultdict(list)
    for starts_at, race_id, player_name, result_id, raw_result_json, source_record_key in source_rows:
        normalized_name = normalize_player_name_strict(player_name or "")
        snapshot, issues = _parse_rating_snapshot(raw_result_json)
        if starts_at is None:
            issues = (*issues, "starts_at_missing")
        rows_by_name[normalized_name].append(
            LegacyRatingChainRow(
                normalized_player_name=normalized_name,
                race_id=int(race_id),
                result_id=int(result_id),
                starts_at=starts_at,
                source_record_key=str(source_record_key),
                snapshot=snapshot,
                issues=issues,
            )
        )
    return {
        normalized_name: tuple(sorted(rows, key=legacy_rating_chain_row_sort_key))
        for normalized_name, rows in rows_by_name.items()
    }


def legacy_rating_chain_issues(
    rows: tuple[LegacyRatingChainRow, ...],
    *,
    recalculation_boundary_source_keys: Collection[str] = (),
    require_initial_zero: bool = True,
) -> tuple[str, ...]:
    if not rows:
        return ()
    boundary_keys = frozenset(recalculation_boundary_source_keys)
    row_keys = {row.source_record_key for row in rows}
    issues = [f"{row.source_record_key}:{issue}" for row in rows for issue in row.issues]
    if not issues:
        if require_initial_zero and rows[0].snapshot["rating_before"] != 0:
            issues.append(f"{rows[0].source_record_key}:initial_rating_not_zero")
        seen_race_ids: set[int] = set()
        for index, row in enumerate(rows):
            if row.race_id in seen_race_ids:
                issues.append(f"{row.source_record_key}:duplicate_game_account_race")
            seen_race_ids.add(row.race_id)
            if (
                index
                and row.source_record_key not in boundary_keys
                and rows[index - 1].snapshot["rating_after"] != row.snapshot["rating_before"]
            ):
                issues.append(f"{row.source_record_key}:rating_chain_discontinuity")
        for missing_key in sorted(boundary_keys - row_keys):
            issues.append(f"{missing_key}:recalculation_boundary_missing")
    return tuple(issues)


def validate_legacy_rating_replay_chains(
    session: Session,
    *,
    source_identifier: str,
    replay_targets_by_name: Mapping[str, int],
    operator_notes_by_name: Mapping[str, str | None],
    replay_modes_by_name: Mapping[str, str] | None = None,
) -> None:
    rows_by_name = load_legacy_rating_chain_rows_by_name(
        session,
        source_identifier=source_identifier,
    )
    modes_by_name = replay_modes_by_name or {}
    if set(modes_by_name) - set(replay_targets_by_name):
        raise LegacyImportError("legacy identity mapping replay mode has no replay target")
    for normalized_name in replay_targets_by_name:
        mode = modes_by_name.get(normalized_name, RATING_REPLAY_SOURCE_EXACT)
        if mode not in RATING_REPLAY_MODES:
            raise LegacyImportError(f"legacy identity mapping replay mode is unsupported: {normalized_name}")
        rows = rows_by_name.get(normalized_name, ())
        issues = legacy_rating_chain_issues(rows, require_initial_zero=False)
        if issues:
            raise LegacyImportError(
                f"legacy identity mapping source-name Rating chain is incomplete for {normalized_name}: {issues[0]}"
            )
        if mode == RATING_REPLAY_RECALCULATE and not rows:
            raise LegacyImportError(
                f"legacy identity mapping recalculation boundary has no Rating rows: {normalized_name}"
            )

    names_by_account: dict[int, list[str]] = defaultdict(list)
    for normalized_name, game_account_id in replay_targets_by_name.items():
        names_by_account[game_account_id].append(normalized_name)

    for game_account_id, normalized_names in names_by_account.items():
        if len(normalized_names) > 1 and any(operator_notes_by_name.get(name) is None for name in normalized_names):
            raise LegacyImportError(
                f"legacy identity mapping alias replay requires operator notes: GameAccount {game_account_id}"
            )
        chain_rows = tuple(
            sorted(
                (row for name in normalized_names for row in rows_by_name.get(name, ())),
                key=legacy_rating_chain_row_sort_key,
            )
        )
        boundary_keys = {
            rows_by_name[name][0].source_record_key
            for name in normalized_names
            if modes_by_name.get(name, RATING_REPLAY_SOURCE_EXACT) == RATING_REPLAY_RECALCULATE
            and rows_by_name.get(name)
        }
        issues = legacy_rating_chain_issues(
            chain_rows,
            recalculation_boundary_source_keys=boundary_keys,
        )
        if issues:
            raise LegacyImportError(
                f"legacy identity mapping replay chain is incomplete for GameAccount {game_account_id}: {issues[0]}"
            )


def legacy_rating_chain_row_sort_key(row: LegacyRatingChainRow) -> tuple[datetime, int, str]:
    starts_at = row.starts_at
    if starts_at is None:
        starts_at = datetime.min.replace(tzinfo=UTC)
    elif starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=UTC)
    else:
        starts_at = starts_at.astimezone(UTC)
    return starts_at, row.race_id, row.source_record_key


def _parse_rating_snapshot(raw_result_json: object) -> tuple[dict[str, Decimal], tuple[str, ...]]:
    raw = raw_result_json if isinstance(raw_result_json, Mapping) else {}
    legacy_rating = raw.get("legacy_rating")
    if not isinstance(legacy_rating, Mapping):
        return {}, ("legacy_rating_missing",)
    snapshot: dict[str, Decimal] = {}
    issues: list[str] = []
    for field in RATING_SNAPSHOT_FIELDS:
        value = legacy_rating.get(field)
        if value is None or isinstance(value, bool):
            issues.append(f"{field}_missing")
            continue
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError):
            issues.append(f"{field}_invalid")
            continue
        if not parsed.is_finite():
            issues.append(f"{field}_invalid")
            continue
        snapshot[field] = parsed
    return snapshot, tuple(issues)


def _rating_value(row: LegacyRatingChainRow, field: str) -> str | None:
    value = row.snapshot.get(field)
    return format(value, "f") if value is not None else None
