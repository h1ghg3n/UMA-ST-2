from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
from typing import Any

from umacircle_bot.domain.betting import (
    MAX_PAYOUT_RATE,
    MAX_POINT_AMOUNT,
    normalize_match_bet_numbers,
)
from umacircle_bot.domain.errors import BettingRuleError
from umacircle_bot.sheets.row_parsers import RoomPointSourceRow, RowParseIssue, RowParseReport

LEGACY_BET_TYPE_MAP = {
    "단승": "win",
    "복승": "quinella",
    "삼복승": "trio",
}
LEGACY_GRANT_TYPE = "지급"
LEGACY_GRANT_RACE_LABEL = "추가 지급"


class LegacyLedgerEntryKind(StrEnum):
    BET = "bet"
    GRANT = "grant"


@dataclass(frozen=True)
class LegacyRoomLedgerRow:
    row_number: int
    source_key: str
    fingerprint: str
    occurred_at: datetime | None
    race_label: str
    participant_name: str
    entry_kind: LegacyLedgerEntryKind
    bet_type: str | None
    numbers: tuple[int, ...]
    amount: int
    is_cancelled: bool
    balance_before: int | None
    payout_rate: Decimal
    payout_amount: int
    balance_after: int | None
    net_change: int


@dataclass(frozen=True)
class LegacyRoomRaceSourceRow:
    row_number: int
    external_race_id: int
    raced_at: datetime
    race_name: str
    grade: str
    race_label: str
    venue: str
    track_surface: str
    distance: int
    direction: str
    season: str
    weather: str
    track_condition: str
    condition_label: str
    participant_count: int


@dataclass(frozen=True)
class LegacyRoomPayoutSourceRow:
    row_number: int
    external_race_id: int
    occurred_at: datetime | None
    race_label: str
    entry_kind: LegacyLedgerEntryKind
    bet_type: str | None
    numbers: tuple[int, ...]
    payout_rate: Decimal


@dataclass(frozen=True)
class LegacyPointMovement:
    source_key: str
    source_row_number: int | None
    participant_name: str
    discord_user_id: str
    race_external_id: int | None
    movement_type: str
    amount: int
    occurred_at: datetime | None


@dataclass(frozen=True)
class LegacyIdentityReconciliation:
    participant_name: str
    discord_user_id: str
    ledger_row_count: int
    cancelled_row_count: int
    opening_balance: int
    reconstructed_balance: int
    current_balance: int


@dataclass(frozen=True)
class LegacyLedgerReconciliationIssue:
    severity: str
    code: str
    message: str
    row_number: int | None = None


@dataclass(frozen=True)
class LegacyRoomLedgerReconciliation:
    active_bet_count: int
    cancelled_bet_count: int
    grant_count: int
    matched_user_count: int
    matched_race_count: int
    identities: tuple[LegacyIdentityReconciliation, ...]
    movements: tuple[LegacyPointMovement, ...]
    issues: tuple[LegacyLedgerReconciliationIssue, ...]

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(issue.severity == "warning" for issue in self.issues)


def parse_legacy_room_ledger_rows(
    rows: Iterable[Sequence[Any]],
    *,
    sheet_name: str = "베팅 Data",
    start_row: int = 1,
) -> RowParseReport[LegacyRoomLedgerRow]:
    normalized_sheet_name = _parse_required_text(sheet_name, field_name="sheet_name", max_length=100)
    parsed: list[LegacyRoomLedgerRow] = []
    issues: list[RowParseIssue] = []
    for row_number, row in _enumerate_rows(rows, start_row=start_row):
        if row_number == start_row or _is_blank_ledger_row(row):
            continue
        try:
            parsed.append(_parse_legacy_room_ledger_row(row_number, row, sheet_name=normalized_sheet_name))
        except (BettingRuleError, ValueError) as exc:
            issues.append(_row_issue(row_number, str(exc), sheet_name=normalized_sheet_name))
    return RowParseReport(rows=tuple(parsed), issues=tuple(issues))


def parse_legacy_room_race_rows(
    rows: Iterable[Sequence[Any]],
    *,
    sheet_name: str = "룸매치 Data",
    start_row: int = 1,
) -> RowParseReport[LegacyRoomRaceSourceRow]:
    normalized_sheet_name = _parse_required_text(sheet_name, field_name="sheet_name", max_length=100)
    parsed: list[LegacyRoomRaceSourceRow] = []
    issues: list[RowParseIssue] = []
    for row_number, row in _enumerate_rows(rows, start_row=start_row):
        if row_number == start_row or _is_blank_race_row(row):
            continue
        try:
            raced_at = _parse_datetime(_cell(row, 0), field_name="raced_at")
            race_name = _parse_required_text(_cell(row, 1), field_name="race_name", max_length=200)
            grade = _parse_required_text(_cell(row, 2), field_name="grade", max_length=16)
            venue = _parse_required_text(_cell(row, 3), field_name="venue", max_length=64)
            track_surface = _parse_required_text(_cell(row, 4), field_name="track_surface", max_length=32)
            distance = _parse_int(_cell(row, 5), field_name="distance", minimum=1)
            direction = _parse_required_text(_cell(row, 6), field_name="direction", max_length=16)
            season = _parse_required_text(_cell(row, 7), field_name="season", max_length=16)
            weather = _parse_required_text(_cell(row, 8), field_name="weather", max_length=32)
            track_condition = _parse_required_text(_cell(row, 9), field_name="track_condition", max_length=32)
            condition_label = _parse_required_text(_cell(row, 10), field_name="condition_label", max_length=32)
            participant_count = _parse_int(_cell(row, 11), field_name="participant_count", minimum=1)
            external_race_id = _parse_int(
                _cell(row, 12),
                field_name="external_race_id",
                minimum=1,
            )
            parsed.append(
                LegacyRoomRaceSourceRow(
                    row_number=row_number,
                    external_race_id=external_race_id,
                    raced_at=raced_at,
                    race_name=race_name,
                    grade=grade,
                    race_label=f"{race_name} ({grade})",
                    venue=venue,
                    track_surface=track_surface,
                    distance=distance,
                    direction=direction,
                    season=season,
                    weather=weather,
                    track_condition=track_condition,
                    condition_label=condition_label,
                    participant_count=participant_count,
                )
            )
        except ValueError as exc:
            issues.append(_row_issue(row_number, str(exc), sheet_name=normalized_sheet_name))
    return RowParseReport(rows=tuple(parsed), issues=tuple(issues))


def parse_legacy_room_payout_rows(
    rows: Iterable[Sequence[Any]],
    *,
    sheet_name: str = "정답배당",
    start_row: int = 1,
) -> RowParseReport[LegacyRoomPayoutSourceRow]:
    normalized_sheet_name = _parse_required_text(sheet_name, field_name="sheet_name", max_length=100)
    parsed: list[LegacyRoomPayoutSourceRow] = []
    issues: list[RowParseIssue] = []
    for row_number, row in _enumerate_rows(rows, start_row=start_row):
        if row_number == start_row or _is_blank_payout_row(row):
            continue
        try:
            type_label = _parse_required_text(_cell(row, 3), field_name="type_label", max_length=32)
            entry_kind, bet_type, numbers = _parse_entry_identity(type_label, _cell(row, 4))
            race_label = _parse_required_text(_cell(row, 2), field_name="race_label", max_length=200)
            external_race_id = _parse_int(
                _cell(row, 0),
                field_name="external_race_id",
                minimum=0 if entry_kind is LegacyLedgerEntryKind.GRANT else 1,
            )
            occurred_at = _parse_optional_datetime(_cell(row, 1), field_name="occurred_at")
            if entry_kind is LegacyLedgerEntryKind.BET and occurred_at is None:
                raise ValueError("occurred_at is required for a bet payout")
            if entry_kind is LegacyLedgerEntryKind.GRANT and (
                external_race_id != 0 or race_label != LEGACY_GRANT_RACE_LABEL
            ):
                raise ValueError("grant payout row has an unsupported identity")
            parsed.append(
                LegacyRoomPayoutSourceRow(
                    row_number=row_number,
                    external_race_id=external_race_id,
                    occurred_at=occurred_at,
                    race_label=race_label,
                    entry_kind=entry_kind,
                    bet_type=bet_type,
                    numbers=numbers,
                    payout_rate=_parse_payout_rate(_cell(row, 5)),
                )
            )
        except (BettingRuleError, ValueError) as exc:
            issues.append(_row_issue(row_number, str(exc), sheet_name=normalized_sheet_name))
    return RowParseReport(rows=tuple(parsed), issues=tuple(issues))


def reconcile_legacy_room_ledger(
    ledger_rows: Sequence[LegacyRoomLedgerRow],
    point_rows: Sequence[RoomPointSourceRow],
    race_rows: Sequence[LegacyRoomRaceSourceRow],
    payout_rows: Sequence[LegacyRoomPayoutSourceRow],
) -> LegacyRoomLedgerReconciliation:
    issues: list[LegacyLedgerReconciliationIssue] = []
    points_by_name = _index_unique(
        point_rows,
        key=lambda row: row.nickname,
        duplicate_code="duplicate_point_nickname",
        duplicate_label="point nickname",
        issues=issues,
    )
    races_by_label = _index_unique(
        race_rows,
        key=lambda row: row.race_label,
        duplicate_code="duplicate_race_label",
        duplicate_label="race label",
        issues=issues,
    )
    _check_duplicate_race_ids(race_rows, issues)
    payouts_by_key = _index_payouts(payout_rows, issues)
    _check_payout_races(payout_rows, races_by_label, issues)
    _check_duplicate_source_keys(ledger_rows, issues)

    rows_by_participant: dict[str, list[LegacyRoomLedgerRow]] = defaultdict(list)
    matched_race_labels: set[str] = set()
    for row in sorted(ledger_rows, key=lambda item: item.row_number):
        rows_by_participant[row.participant_name].append(row)
        if row.participant_name not in points_by_name:
            issues.append(
                _reconciliation_issue(
                    "unmatched_identity",
                    f"ledger participant has no unique point account: {row.participant_name}",
                    row_number=row.row_number,
                )
            )
        if row.entry_kind is LegacyLedgerEntryKind.BET:
            race = races_by_label.get(row.race_label)
            if race is None:
                issues.append(
                    _reconciliation_issue(
                        "unmatched_race",
                        f"ledger race has no unique race metadata row: {row.race_label}",
                        row_number=row.row_number,
                    )
                )
            else:
                matched_race_labels.add(row.race_label)
        _check_ledger_payout(row, payouts_by_key, issues)

    identities: list[LegacyIdentityReconciliation] = []
    movements: list[LegacyPointMovement] = []
    for participant_name in sorted(rows_by_participant):
        point_row = points_by_name.get(participant_name)
        if point_row is None:
            continue
        participant_rows = rows_by_participant[participant_name]
        active_rows = [row for row in participant_rows if not row.is_cancelled]
        if not active_rows:
            issues.append(
                _reconciliation_issue(
                    "missing_active_ledger_row",
                    f"point account has no active ledger row: {participant_name}",
                )
            )
            continue
        opening_balance = active_rows[0].balance_before
        if opening_balance is None:
            raise AssertionError("parsed active ledger row is missing balance_before")
        _check_balance_continuity(participant_name, active_rows, issues)
        reconstructed_balance = active_rows[-1].balance_after
        if reconstructed_balance is None:
            raise AssertionError("parsed active ledger row is missing balance_after")
        if reconstructed_balance != point_row.points:
            issues.append(
                _reconciliation_issue(
                    "ending_balance_mismatch",
                    (
                        f"reconstructed balance for {participant_name} is {reconstructed_balance}, "
                        f"but point sheet has {point_row.points}"
                    ),
                    row_number=active_rows[-1].row_number,
                )
            )
        identities.append(
            LegacyIdentityReconciliation(
                participant_name=participant_name,
                discord_user_id=point_row.discord_user_id,
                ledger_row_count=len(participant_rows),
                cancelled_row_count=sum(row.is_cancelled for row in participant_rows),
                opening_balance=opening_balance,
                reconstructed_balance=reconstructed_balance,
                current_balance=point_row.points,
            )
        )
        movements.append(
            LegacyPointMovement(
                source_key=f"legacy:opening:{point_row.discord_user_id}",
                source_row_number=None,
                participant_name=participant_name,
                discord_user_id=point_row.discord_user_id,
                race_external_id=None,
                movement_type="legacy_opening_balance",
                amount=opening_balance,
                occurred_at=None,
            )
        )
        movements.extend(
            _build_row_movements(
                active_rows,
                point_row=point_row,
                races_by_label=races_by_label,
            )
        )

    for point_row in point_rows:
        if point_row.nickname not in rows_by_participant:
            issues.append(
                _reconciliation_issue(
                    "point_account_without_ledger",
                    f"point account has no ledger history: {point_row.nickname}",
                    row_number=point_row.row_number,
                )
            )

    return LegacyRoomLedgerReconciliation(
        active_bet_count=sum(
            row.entry_kind is LegacyLedgerEntryKind.BET and not row.is_cancelled for row in ledger_rows
        ),
        cancelled_bet_count=sum(
            row.entry_kind is LegacyLedgerEntryKind.BET and row.is_cancelled for row in ledger_rows
        ),
        grant_count=sum(row.entry_kind is LegacyLedgerEntryKind.GRANT for row in ledger_rows),
        matched_user_count=len(identities),
        matched_race_count=len(matched_race_labels),
        identities=tuple(identities),
        movements=tuple(movements),
        issues=tuple(issues),
    )


def _parse_legacy_room_ledger_row(
    row_number: int,
    row: Sequence[Any],
    *,
    sheet_name: str,
) -> LegacyRoomLedgerRow:
    race_label = _parse_required_text(_cell(row, 1), field_name="race_label", max_length=200)
    participant_name = _parse_required_text(_cell(row, 2), field_name="participant_name", max_length=100)
    type_label = _parse_required_text(_cell(row, 3), field_name="type_label", max_length=32)
    entry_kind, bet_type, numbers = _parse_entry_identity(type_label, _cell(row, 4))
    occurred_at = _parse_optional_datetime(_cell(row, 0), field_name="occurred_at")
    if entry_kind is LegacyLedgerEntryKind.BET and occurred_at is None:
        raise ValueError("occurred_at is required for a bet")
    if entry_kind is LegacyLedgerEntryKind.GRANT and race_label != LEGACY_GRANT_RACE_LABEL:
        raise ValueError("grant row has an unsupported race label")

    amount = _parse_int(_cell(row, 5), field_name="amount", minimum=1)
    is_cancelled = _parse_bool(_cell(row, 6), field_name="is_cancelled")
    if entry_kind is LegacyLedgerEntryKind.GRANT and is_cancelled:
        raise ValueError("grant row cannot be cancelled")
    payout_rate = _parse_payout_rate(_cell(row, 8))
    balance_before = _parse_optional_int(_cell(row, 7), field_name="balance_before", minimum=0)
    balance_after = _parse_optional_int(_cell(row, 9), field_name="balance_after", minimum=0)

    if is_cancelled:
        if balance_before is not None or balance_after is not None:
            raise ValueError("cancelled row must not change a cached balance")
        payout_amount = 0
        net_change = 0
    else:
        if balance_before is None or balance_after is None:
            raise ValueError("active row requires cached before and after balances")
        expected_after = (Decimal(balance_before) - Decimal(amount) + Decimal(amount) * payout_rate).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        if expected_after != Decimal(balance_after):
            raise ValueError("cached balance does not match the legacy Excel ROUND calculation")
        net_change = balance_after - balance_before
        if entry_kind is LegacyLedgerEntryKind.GRANT:
            if net_change != amount:
                raise ValueError("grant row does not increase the balance by its amount")
            payout_amount = 0
        else:
            payout_amount = net_change + amount
            if not 0 <= payout_amount <= MAX_POINT_AMOUNT:
                raise ValueError("derived payout amount is outside the supported range")

    source_key = f"{sheet_name}:{row_number}"
    fingerprint = _ledger_fingerprint(
        occurred_at=occurred_at,
        race_label=race_label,
        participant_name=participant_name,
        entry_kind=entry_kind,
        bet_type=bet_type,
        numbers=numbers,
        amount=amount,
        is_cancelled=is_cancelled,
        balance_before=balance_before,
        payout_rate=payout_rate,
        balance_after=balance_after,
    )
    return LegacyRoomLedgerRow(
        row_number=row_number,
        source_key=source_key,
        fingerprint=fingerprint,
        occurred_at=occurred_at,
        race_label=race_label,
        participant_name=participant_name,
        entry_kind=entry_kind,
        bet_type=bet_type,
        numbers=numbers,
        amount=amount,
        is_cancelled=is_cancelled,
        balance_before=balance_before,
        payout_rate=payout_rate,
        payout_amount=payout_amount,
        balance_after=balance_after,
        net_change=net_change,
    )


def _parse_entry_identity(
    type_label: str,
    selection_value: Any,
) -> tuple[LegacyLedgerEntryKind, str | None, tuple[int, ...]]:
    if type_label == LEGACY_GRANT_TYPE:
        selection = _parse_selection_text(selection_value)
        if selection != "0":
            raise ValueError("grant selection must be 0")
        return LegacyLedgerEntryKind.GRANT, None, ()
    bet_type = LEGACY_BET_TYPE_MAP.get(type_label)
    if bet_type is None:
        raise ValueError("unsupported legacy bet type")
    selection = _parse_selection_text(selection_value)
    try:
        raw_numbers = [_parse_ascii_positive_int(part) for part in selection.split("-")]
        numbers = normalize_match_bet_numbers(bet_type, raw_numbers)
    except BettingRuleError as exc:
        raise ValueError(str(exc)) from exc
    return LegacyLedgerEntryKind.BET, bet_type, tuple(numbers)


def _build_row_movements(
    rows: Sequence[LegacyRoomLedgerRow],
    *,
    point_row: RoomPointSourceRow,
    races_by_label: dict[str, LegacyRoomRaceSourceRow],
) -> list[LegacyPointMovement]:
    movements: list[LegacyPointMovement] = []
    for row in rows:
        if row.entry_kind is LegacyLedgerEntryKind.GRANT:
            movements.append(
                LegacyPointMovement(
                    source_key=f"{row.source_key}:grant",
                    source_row_number=row.row_number,
                    participant_name=row.participant_name,
                    discord_user_id=point_row.discord_user_id,
                    race_external_id=None,
                    movement_type="admin_grant",
                    amount=row.amount,
                    occurred_at=row.occurred_at,
                )
            )
            continue
        race = races_by_label.get(row.race_label)
        race_external_id = race.external_race_id if race is not None else None
        movements.append(
            LegacyPointMovement(
                source_key=f"{row.source_key}:stake",
                source_row_number=row.row_number,
                participant_name=row.participant_name,
                discord_user_id=point_row.discord_user_id,
                race_external_id=race_external_id,
                movement_type="bet_stake",
                amount=-row.amount,
                occurred_at=row.occurred_at,
            )
        )
        movements.append(
            LegacyPointMovement(
                source_key=f"{row.source_key}:settlement",
                source_row_number=row.row_number,
                participant_name=row.participant_name,
                discord_user_id=point_row.discord_user_id,
                race_external_id=race_external_id,
                movement_type="settlement_reward",
                amount=row.payout_amount,
                occurred_at=row.occurred_at,
            )
        )
    return movements


def _check_balance_continuity(
    participant_name: str,
    rows: Sequence[LegacyRoomLedgerRow],
    issues: list[LegacyLedgerReconciliationIssue],
) -> None:
    previous_after = rows[0].balance_after
    for row in rows[1:]:
        if row.balance_before != previous_after:
            issues.append(
                _reconciliation_issue(
                    "balance_sequence_gap",
                    f"cached balance sequence is discontinuous for {participant_name}",
                    row_number=row.row_number,
                )
            )
        previous_after = row.balance_after


def _check_ledger_payout(
    row: LegacyRoomLedgerRow,
    payouts_by_key: dict[tuple[str, str | None, tuple[int, ...]], LegacyRoomPayoutSourceRow],
    issues: list[LegacyLedgerReconciliationIssue],
) -> None:
    payout_key = (row.race_label, row.bet_type, row.numbers)
    payout = payouts_by_key.get(payout_key)
    expected_rate = payout.payout_rate if payout is not None else Decimal(0)
    if row.payout_rate != expected_rate:
        issues.append(
            _reconciliation_issue(
                "payout_rate_mismatch",
                (f"cached payout rate {row.payout_rate} does not match payout sheet rate {expected_rate}"),
                row_number=row.row_number,
                severity="warning",
            )
        )


def _index_payouts(
    payout_rows: Sequence[LegacyRoomPayoutSourceRow],
    issues: list[LegacyLedgerReconciliationIssue],
) -> dict[tuple[str, str | None, tuple[int, ...]], LegacyRoomPayoutSourceRow]:
    indexed: dict[tuple[str, str | None, tuple[int, ...]], LegacyRoomPayoutSourceRow] = {}
    duplicate_keys: set[tuple[str, str | None, tuple[int, ...]]] = set()
    for row in payout_rows:
        key = (row.race_label, row.bet_type, row.numbers)
        if key in indexed:
            duplicate_keys.add(key)
            issues.append(
                _reconciliation_issue(
                    "duplicate_payout_definition",
                    f"duplicate payout definition: {row.race_label}",
                    row_number=row.row_number,
                )
            )
        else:
            indexed[key] = row
    for key in duplicate_keys:
        indexed.pop(key, None)
    return indexed


def _check_payout_races(
    payout_rows: Sequence[LegacyRoomPayoutSourceRow],
    races_by_label: dict[str, LegacyRoomRaceSourceRow],
    issues: list[LegacyLedgerReconciliationIssue],
) -> None:
    for payout in payout_rows:
        if payout.entry_kind is LegacyLedgerEntryKind.GRANT:
            continue
        race = races_by_label.get(payout.race_label)
        if race is None:
            issues.append(
                _reconciliation_issue(
                    "unmatched_payout_race",
                    f"payout row has no unique race metadata row: {payout.race_label}",
                    row_number=payout.row_number,
                )
            )
        elif race.external_race_id != payout.external_race_id:
            issues.append(
                _reconciliation_issue(
                    "payout_race_id_mismatch",
                    f"payout race ID does not match race metadata: {payout.race_label}",
                    row_number=payout.row_number,
                )
            )


def _check_duplicate_source_keys(
    ledger_rows: Sequence[LegacyRoomLedgerRow],
    issues: list[LegacyLedgerReconciliationIssue],
) -> None:
    seen: set[str] = set()
    for row in ledger_rows:
        if row.source_key in seen:
            issues.append(
                _reconciliation_issue(
                    "duplicate_source_key",
                    f"duplicate ledger source key: {row.source_key}",
                    row_number=row.row_number,
                )
            )
        seen.add(row.source_key)


def _check_duplicate_race_ids(
    race_rows: Sequence[LegacyRoomRaceSourceRow],
    issues: list[LegacyLedgerReconciliationIssue],
) -> None:
    seen: set[int] = set()
    for row in race_rows:
        if row.external_race_id in seen:
            issues.append(
                _reconciliation_issue(
                    "duplicate_external_race_id",
                    f"duplicate external race ID: {row.external_race_id}",
                    row_number=row.row_number,
                )
            )
        seen.add(row.external_race_id)


def _index_unique(
    rows: Sequence[Any],
    *,
    key: Callable[[Any], str],
    duplicate_code: str,
    duplicate_label: str,
    issues: list[LegacyLedgerReconciliationIssue],
) -> dict[str, Any]:
    indexed: dict[str, Any] = {}
    duplicate_keys: set[str] = set()
    for row in rows:
        value = key(row)
        if value in indexed:
            duplicate_keys.add(value)
            issues.append(
                _reconciliation_issue(
                    duplicate_code,
                    f"duplicate {duplicate_label}: {value}",
                    row_number=row.row_number,
                )
            )
        else:
            indexed[value] = row
    for value in duplicate_keys:
        indexed.pop(value, None)
    return indexed


def _ledger_fingerprint(**values: Any) -> str:
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _parse_selection_text(value: Any) -> str:
    normalized = _normalize_blank(value)
    if normalized is None or isinstance(normalized, bool):
        raise ValueError("selection is required")
    if isinstance(normalized, int):
        return str(normalized)
    if isinstance(normalized, float) and normalized.is_integer():
        return str(int(normalized))
    if isinstance(normalized, Decimal) and normalized == normalized.to_integral_value():
        return str(int(normalized))
    if not isinstance(normalized, str):
        raise ValueError("selection must contain ASCII integer values")
    if len(normalized) > 64 or any(ord(character) < 32 for character in normalized):
        raise ValueError("selection contains unsupported text")
    return normalized


def _parse_ascii_positive_int(value: str) -> int:
    if not value or len(value) > 10 or not value.isascii() or not value.isdigit():
        raise ValueError("selection must contain hyphen-separated positive ASCII integers")
    parsed = int(value)
    if not 1 <= parsed <= MAX_POINT_AMOUNT:
        raise ValueError("selection number is outside the supported range")
    return parsed


def _parse_payout_rate(value: Any) -> Decimal:
    normalized = _normalize_blank(value)
    if normalized is None or isinstance(normalized, bool):
        raise ValueError("payout_rate is required and must be numeric")
    numeric_text = str(normalized)
    if len(numeric_text) > 64:
        raise ValueError("payout_rate has unsupported precision")
    try:
        parsed = Decimal(numeric_text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("payout_rate must be numeric") from exc
    if not parsed.is_finite() or not 0 <= parsed <= MAX_PAYOUT_RATE:
        raise ValueError("payout_rate is outside the supported range")
    decimal_tuple = parsed.as_tuple()
    if len(decimal_tuple.digits) > 32 or decimal_tuple.exponent < -18:
        raise ValueError("payout_rate has unsupported precision")
    return parsed


def _parse_datetime(value: Any, *, field_name: str) -> datetime:
    parsed = _parse_optional_datetime(value, field_name=field_name)
    if parsed is None:
        raise ValueError(f"{field_name} is required")
    return parsed


def _parse_optional_datetime(value: Any, *, field_name: str) -> datetime | None:
    normalized = _normalize_blank(value)
    if normalized is None:
        return None
    if not isinstance(normalized, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    return normalized


def _parse_required_text(value: Any, *, field_name: str, max_length: int) -> str:
    normalized = _normalize_blank(value)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    if not isinstance(normalized, str):
        normalized = str(normalized).strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    if len(normalized) > max_length or any(ord(character) < 32 for character in normalized):
        raise ValueError(f"{field_name} contains unsupported text")
    return normalized


def _parse_int(value: Any, *, field_name: str, minimum: int) -> int:
    parsed = _parse_optional_int(value, field_name=field_name, minimum=minimum)
    if parsed is None:
        raise ValueError(f"{field_name} is required")
    return parsed


def _parse_optional_int(value: Any, *, field_name: str, minimum: int) -> int | None:
    normalized = _normalize_blank(value)
    if normalized is None:
        return None
    if isinstance(normalized, bool):
        raise ValueError(f"{field_name} must be an integer")
    if isinstance(normalized, int):
        parsed = normalized
    elif isinstance(normalized, float) and normalized.is_integer():
        parsed = int(normalized)
    elif isinstance(normalized, Decimal) and normalized == normalized.to_integral_value():
        parsed = int(normalized)
    elif isinstance(normalized, str) and len(normalized) <= 10 and normalized.isascii() and normalized.isdigit():
        parsed = int(normalized)
    else:
        raise ValueError(f"{field_name} must be an integer")
    if not minimum <= parsed <= MAX_POINT_AMOUNT:
        raise ValueError(f"{field_name} must be between {minimum} and {MAX_POINT_AMOUNT}")
    return parsed


def _parse_bool(value: Any, *, field_name: str) -> bool:
    normalized = _normalize_blank(value)
    if isinstance(normalized, bool):
        return normalized
    if isinstance(normalized, int) and normalized in {0, 1}:
        return bool(normalized)
    if isinstance(normalized, str):
        lowered = normalized.lower()
        if lowered in {"true", "yes", "y", "1"}:
            return True
        if lowered in {"false", "no", "n", "0"}:
            return False
    raise ValueError(f"{field_name} must be boolean")


def _normalize_blank(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _enumerate_rows(rows: Iterable[Sequence[Any]], *, start_row: int) -> Iterable[tuple[int, Sequence[Any]]]:
    if not isinstance(start_row, int) or isinstance(start_row, bool) or start_row <= 0:
        raise ValueError("start_row must be a positive integer")
    for offset, row in enumerate(rows):
        yield start_row + offset, row


def _cell(row: Sequence[Any], index: int) -> Any:
    return row[index] if index < len(row) else None


def _is_blank_ledger_row(row: Sequence[Any]) -> bool:
    return all(_normalize_blank(_cell(row, index)) is None for index in range(1, 6))


def _is_blank_race_row(row: Sequence[Any]) -> bool:
    return all(_normalize_blank(_cell(row, index)) is None for index in (0, 1, 2, 12))


def _is_blank_payout_row(row: Sequence[Any]) -> bool:
    return all(_normalize_blank(_cell(row, index)) is None for index in range(6))


def _row_issue(row_number: int, message: str, *, sheet_name: str) -> RowParseIssue:
    return RowParseIssue(
        row_number=row_number,
        code="invalid_row",
        message=message,
        sheet_name=sheet_name,
    )


def _reconciliation_issue(
    code: str,
    message: str,
    *,
    row_number: int | None = None,
    severity: str = "error",
) -> LegacyLedgerReconciliationIssue:
    return LegacyLedgerReconciliationIssue(
        severity=severity,
        code=code,
        message=message,
        row_number=row_number,
    )
