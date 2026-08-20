from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from hashlib import sha256

from sqlalchemy import text
from sqlalchemy.engine import Connection

from umacircle_bot.runtime_preflight import EXPECTED_ROOM_POINT_SCALE
from umacircle_bot.services.rebuild_overlay_bundle import (
    LoadedRebuildOverlayBundle,
    RebuildOverlayImportError,
)

ConflictRecorder = Callable[[str, int], None]


def reconcile_rebuild_target(
    connection: Connection,
    *,
    bundle: LoadedRebuildOverlayBundle,
    source_to_target_game: Mapping[int, int],
    add_conflict: ConflictRecorder,
) -> dict[str, object]:
    source_reconciliation = bundle.reconciliation
    source_points = _mapping(source_reconciliation.get("room_points"), code="invalid_reconciliation")
    source_room_match = _mapping(source_reconciliation.get("room_match"), code="invalid_reconciliation")
    source_not_operational = _mapping(source_reconciliation.get("not_yet_operational"), code="invalid_reconciliation")
    operational_tables = _tables(bundle.operational_overlay)

    point_result = _reconcile_points(
        connection,
        source_points=source_points,
        operational_tables=operational_tables,
        source_to_target_game=source_to_target_game,
        add_conflict=add_conflict,
    )
    room_match_result = _reconcile_room_match(
        connection,
        source_room_match=source_room_match,
        operational_tables=operational_tables,
        add_conflict=add_conflict,
    )
    monetary_result = _reconcile_scaled_monetary_values(
        connection,
        operational_tables=operational_tables,
        add_conflict=add_conflict,
    )
    not_operational_result = _reconcile_not_yet_operational(
        connection,
        source_not_operational=source_not_operational,
        operational_tables=operational_tables,
        add_conflict=add_conflict,
    )
    return {
        "room_points": point_result,
        "room_match": room_match_result,
        "scaled_monetary_totals": monetary_result,
        "not_yet_operational": not_operational_result,
    }


def _reconcile_points(
    connection: Connection,
    *,
    source_points: Mapping[str, object],
    operational_tables: Mapping[str, Mapping[str, object]],
    source_to_target_game: Mapping[int, int],
    add_conflict: ConflictRecorder,
) -> dict[str, object]:
    source_wallet_rows = _snapshot_rows(operational_tables["room_point_accounts"])
    source_ledger_rows = _snapshot_rows(operational_tables["room_point_transactions"])
    source_wallet_sum = sum(_required_int(row, "balance") for row in source_wallet_rows)
    source_ledger_sum = sum(_required_int(row, "amount") for row in source_ledger_rows)
    if (
        len(source_wallet_rows) != _required_nonnegative_int(source_points, "wallet_count")
        or source_wallet_sum != _required_int(source_points, "wallet_balance_sum")
        or len(source_ledger_rows) != _required_nonnegative_int(source_points, "ledger_row_count")
        or source_ledger_sum != _required_int(source_points, "ledger_amount_sum")
    ):
        raise _error(
            "source_reconciliation_snapshot_mismatch",
            "source Circle Point reconciliation does not match its operational snapshots",
        )

    expected = {
        "wallet_count": len(source_wallet_rows),
        "wallet_balance_sum": source_wallet_sum * EXPECTED_ROOM_POINT_SCALE,
        "ledger_row_count": len(source_ledger_rows),
        "ledger_amount_sum": source_ledger_sum * EXPECTED_ROOM_POINT_SCALE,
    }
    actual = {
        "wallet_count": _scalar_int(connection, "SELECT COUNT(*) FROM room_point_accounts"),
        "wallet_balance_sum": _scalar_int(connection, "SELECT COALESCE(SUM(balance), 0) FROM room_point_accounts"),
        "ledger_row_count": _scalar_int(connection, "SELECT COUNT(*) FROM room_point_transactions"),
        "ledger_amount_sum": _scalar_int(
            connection,
            "SELECT COALESCE(SUM(amount), 0) FROM room_point_transactions",
        ),
    }
    for key in expected:
        if actual[key] != expected[key]:
            add_conflict(f"room_point_{key}_mismatch", 1)
    if actual["wallet_balance_sum"] != actual["ledger_amount_sum"]:
        add_conflict("target_wallet_ledger_mismatch", 1)

    target_game_personas = {
        int(row["id"]): str(row["persona_id"])
        for row in connection.execute(text("SELECT id, persona_id FROM game_accounts")).mappings()
        if row["persona_id"] is not None
    }
    expected_wallets: defaultdict[str, int] = defaultdict(int)
    wallet_mapping_errors = 0
    for source_row in source_wallet_rows:
        source_game_id = _required_int(source_row, "game_account_id")
        target_game_id = source_to_target_game.get(source_game_id)
        target_persona_id = target_game_personas.get(target_game_id) if target_game_id is not None else None
        if target_persona_id is None:
            wallet_mapping_errors += 1
            continue
        expected_wallets[target_persona_id] += _required_int(source_row, "balance") * EXPECTED_ROOM_POINT_SCALE
    add_conflict("room_point_wallet_owner_mapping_missing", wallet_mapping_errors)
    actual_wallets: dict[str, int] = {}
    duplicate_wallets = 0
    for row in connection.execute(text("SELECT persona_id, balance FROM room_point_accounts")).mappings():
        persona_id = str(row["persona_id"])
        if persona_id in actual_wallets:
            duplicate_wallets += 1
        actual_wallets[persona_id] = int(row["balance"])
    add_conflict("room_point_wallet_owner_duplicate", duplicate_wallets)
    wallet_ownership_matches = (
        not wallet_mapping_errors and not duplicate_wallets and dict(expected_wallets) == actual_wallets
    )
    if not wallet_ownership_matches:
        add_conflict("room_point_wallet_owner_balance_mismatch", 1)

    expected_ledger: Counter[tuple[object, ...]] = Counter()
    ledger_mapping_errors = 0
    for source_row in source_ledger_rows:
        source_game_id = _required_int(source_row, "game_account_id")
        target_game_id = source_to_target_game.get(source_game_id)
        target_persona_id = target_game_personas.get(target_game_id) if target_game_id is not None else None
        if target_game_id is None or target_persona_id is None:
            ledger_mapping_errors += 1
            continue
        expected_ledger[
            (
                target_game_id,
                target_persona_id,
                source_row.get("source"),
                source_row.get("type"),
                _required_int(source_row, "amount") * EXPECTED_ROOM_POINT_SCALE,
            )
        ] += 1
    add_conflict("room_point_ledger_owner_mapping_missing", ledger_mapping_errors)
    actual_ledger: Counter[tuple[object, ...]] = Counter()
    non_scaled_ledger_rows = 0
    for row in connection.execute(
        text("SELECT game_account_id, persona_id, source, type, amount FROM room_point_transactions")
    ).mappings():
        amount = int(row["amount"])
        if amount % EXPECTED_ROOM_POINT_SCALE:
            non_scaled_ledger_rows += 1
        actual_ledger[
            (
                int(row["game_account_id"]),
                str(row["persona_id"]),
                row["source"],
                row["type"],
                amount,
            )
        ] += 1
    add_conflict("room_point_ledger_non_scaled_value", non_scaled_ledger_rows)
    ledger_ownership_matches = not ledger_mapping_errors and expected_ledger == actual_ledger
    if not ledger_ownership_matches:
        add_conflict("room_point_ledger_owner_value_mismatch", 1)
    _reconcile_point_provenance(connection, source_points=source_points, add_conflict=add_conflict)

    aggregate_matches = expected == actual
    return {
        "expected": expected,
        "actual": actual,
        "aggregate_matches": aggregate_matches,
        "wallet_ownership_matches": wallet_ownership_matches,
        "ledger_ownership_matches": ledger_ownership_matches,
        "matches": aggregate_matches and wallet_ownership_matches and ledger_ownership_matches,
    }


def _reconcile_point_provenance(
    connection: Connection,
    *,
    source_points: Mapping[str, object],
    add_conflict: ConflictRecorder,
) -> None:
    source_groups = _sequence(source_points.get("source_type_totals"), code="invalid_reconciliation")
    expected: dict[str, tuple[int, int]] = {}
    for value in source_groups:
        row = _mapping(value, code="invalid_reconciliation")
        key = _normalize_sha256(row.get("provenance_group_sha256"), code="invalid_reconciliation")
        expected[key] = (
            _required_nonnegative_int(row, "row_count"),
            _required_int(row, "amount_sum") * EXPECTED_ROOM_POINT_SCALE,
        )
    actual: dict[str, tuple[int, int]] = {}
    rows = connection.execute(
        text(
            "SELECT source, type, COUNT(*) AS row_count, COALESCE(SUM(amount), 0) AS amount_sum "
            "FROM room_point_transactions GROUP BY source, type ORDER BY source, type"
        )
    ).mappings()
    for row in rows:
        key = _canonical_sha256({"source": row["source"], "type": row["type"]})
        actual[key] = (int(row["row_count"]), int(row["amount_sum"]))
    if actual != expected:
        add_conflict("room_point_provenance_totals_mismatch", 1)


def _reconcile_room_match(
    connection: Connection,
    *,
    source_room_match: Mapping[str, object],
    operational_tables: Mapping[str, Mapping[str, object]],
    add_conflict: ConflictRecorder,
) -> dict[str, object]:
    count_contract = {
        "race_count": ("races", "SELECT COUNT(*) FROM races"),
        "entry_count": ("race_entries", "SELECT COUNT(*) FROM race_entries"),
        "result_count": ("race_results", "SELECT COUNT(*) FROM race_results"),
        "bet_count": ("bets", "SELECT COUNT(*) FROM bets"),
        "judgement_count": ("bet_judgements", "SELECT COUNT(*) FROM bet_judgements"),
    }
    expected = {
        key: len(_snapshot_rows(operational_tables[table_name]))
        for key, (table_name, _statement) in count_contract.items()
    }
    declared = {key: _required_nonnegative_int(source_room_match, key) for key in count_contract}
    if declared != expected:
        raise _error(
            "source_reconciliation_snapshot_mismatch",
            "source Room Match reconciliation does not match its operational snapshots",
        )
    actual = {key: _scalar_int(connection, statement) for key, (_table_name, statement) in count_contract.items()}
    for key in count_contract:
        if actual[key] != expected[key]:
            add_conflict(f"room_match_{key}_mismatch", 1)
    status_matches = True
    for source_key, table_name, column_name in (
        ("race_status_counts", "races", "status"),
        ("bet_status_counts", "bets", "status"),
        ("judgement_status_counts", "bet_judgements", "judgement_status"),
    ):
        source_status_counts = source_room_match.get(source_key)
        snapshot_status_counts = _snapshot_group_counts(
            _snapshot_rows(operational_tables[table_name]),
            column_name,
        )
        if _group_count_signature(source_status_counts) != _group_count_signature(snapshot_status_counts):
            raise _error(
                "source_reconciliation_snapshot_mismatch",
                "source Room Match status reconciliation does not match its operational snapshots",
            )
        if _group_count_signature(source_status_counts) != _group_count_signature(
            _group_counts(connection, table_name, column_name)
        ):
            add_conflict(f"room_match_{source_key}_mismatch", 1)
            status_matches = False
    return {
        "expected": expected,
        "actual": actual,
        "status_counts_match": status_matches,
        "matches": expected == actual and status_matches,
    }


def _reconcile_scaled_monetary_values(
    connection: Connection,
    *,
    operational_tables: Mapping[str, Mapping[str, object]],
    add_conflict: ConflictRecorder,
) -> dict[str, object]:
    expected_sums: dict[str, int] = {}
    actual_sums: dict[str, int] = {}
    multisets_match = True
    all_values_scaled = True
    for table_name, column_name in (
        ("bets", "amount"),
        ("bet_judgements", "stake_amount"),
        ("bet_judgements", "payout_amount"),
        ("bet_judgements", "point_delta"),
    ):
        key = f"{table_name}.{column_name}"
        source_values = [_required_int(row, column_name) for row in _snapshot_rows(operational_tables[table_name])]
        expected_values = Counter(value * EXPECTED_ROOM_POINT_SCALE for value in source_values)
        target_values = [
            int(value) for value in connection.execute(text(f"SELECT {column_name} FROM {table_name}")).scalars()
        ]
        actual_values = Counter(target_values)
        expected_sums[key] = sum(expected_values.elements())
        actual_sums[key] = sum(target_values)
        if actual_values != expected_values:
            add_conflict(f"monetary_{table_name}_{column_name}_multiset_mismatch", 1)
            multisets_match = False
        non_scaled_count = sum(value % EXPECTED_ROOM_POINT_SCALE != 0 for value in target_values)
        add_conflict(f"monetary_{table_name}_{column_name}_non_scaled_value", non_scaled_count)
        all_values_scaled = all_values_scaled and non_scaled_count == 0
    return {
        "expected": expected_sums,
        "actual": actual_sums,
        "multisets_match": multisets_match,
        "all_values_scaled": all_values_scaled,
        "matches": multisets_match and all_values_scaled,
    }


def _reconcile_not_yet_operational(
    connection: Connection,
    *,
    source_not_operational: Mapping[str, object],
    operational_tables: Mapping[str, Mapping[str, object]],
    add_conflict: ConflictRecorder,
) -> dict[str, object]:
    contracts = {
        "win5_entry_count": ("win5_entries", "SELECT COUNT(*) FROM win5_entries"),
        "win5_score_count": ("win5_scores", "SELECT COUNT(*) FROM win5_scores"),
        "rating_rule_version_count": (
            "rating_rule_versions",
            "SELECT COUNT(*) FROM rating_rule_versions",
        ),
        "rating_event_count": ("rating_events", "SELECT COUNT(*) FROM rating_events"),
        "discord_publication_count": (
            "discord_publications",
            "SELECT COUNT(*) FROM discord_publications",
        ),
        "sheet_export_run_count": ("sheet_export_runs", "SELECT COUNT(*) FROM sheet_export_runs"),
        "export_run_count": ("export_runs", "SELECT COUNT(*) FROM export_runs"),
    }
    expected = {
        key: len(_snapshot_rows(operational_tables[table_name])) for key, (table_name, _statement) in contracts.items()
    }
    declared = {key: _required_nonnegative_int(source_not_operational, key) for key in contracts}
    if declared != expected:
        raise _error(
            "source_reconciliation_snapshot_mismatch",
            "source non-operational reconciliation does not match its operational snapshots",
        )
    actual = {key: _scalar_int(connection, statement) for key, (_table_name, statement) in contracts.items()}
    for key in contracts:
        if actual[key] != expected[key]:
            add_conflict(f"not_operational_{key}_mismatch", 1)
    return {"expected": expected, "actual": actual, "matches": expected == actual}


def _snapshot_group_counts(
    rows: Sequence[Mapping[str, object]],
    column_name: str,
) -> list[dict[str, object]]:
    counts = Counter(row.get(column_name) for row in rows)
    return [
        {"value": value, "row_count": row_count}
        for value, row_count in sorted(counts.items(), key=lambda item: _canonical_json(item[0]))
    ]


def _group_count_signature(value: object) -> Counter[tuple[str, int]]:
    result: Counter[tuple[str, int]] = Counter()
    for item_value in _sequence(value, code="invalid_reconciliation"):
        item = _mapping(item_value, code="invalid_reconciliation")
        row_count = _required_nonnegative_int(item, "row_count")
        result[(_canonical_json(item.get("value")), row_count)] += 1
    return result


def _group_counts(connection: Connection, table_name: str, column_name: str) -> list[dict[str, object]]:
    table = connection.dialect.identifier_preparer.quote_identifier(table_name)
    column = connection.dialect.identifier_preparer.quote_identifier(column_name)
    rows = connection.execute(
        text(f"SELECT {column} AS value, COUNT(*) AS row_count FROM {table} GROUP BY {column} ORDER BY {column}")
    ).mappings()
    return [{"value": row["value"], "row_count": int(row["row_count"])} for row in rows]


def _tables(payload: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    return {
        str(name): _mapping(value, code="invalid_snapshot_table_set")
        for name, value in _mapping(payload.get("tables"), code="invalid_snapshot_table_set").items()
    }


def _snapshot_rows(snapshot: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    return tuple(
        _mapping(row, code="invalid_table_snapshot")
        for row in _sequence(snapshot.get("rows"), code="invalid_table_snapshot")
    )


def _scalar_int(connection: Connection, statement: str) -> int:
    return int(connection.execute(text(statement)).scalar_one() or 0)


def _mapping(value: object, *, code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _error(code, "bundle structure is invalid")
    return value


def _sequence(value: object, *, code: str) -> Sequence[object]:
    if not isinstance(value, (list, tuple)):
        raise _error(code, "bundle structure is invalid")
    return value


def _required_int(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool):
        raise _error("invalid_bundle_row", "bundle row contains an invalid required integer")
    return item


def _required_nonnegative_int(value: Mapping[str, object], key: str) -> int:
    item = _required_int(value, key)
    if item < 0:
        raise _error("invalid_reconciliation", "reconciliation contains a negative count")
    return item


def _normalize_sha256(value: object, *, code: str) -> str:
    if not isinstance(value, str):
        raise _error(code, "SHA-256 value is invalid")
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise _error(code, "SHA-256 value is invalid")
    return normalized


def _canonical_sha256(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _error(code: str, message: str) -> RebuildOverlayImportError:
    return RebuildOverlayImportError(code, message)
