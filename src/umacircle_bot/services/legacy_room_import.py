from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

from sqlalchemy.orm import Session

from umacircle_bot.domain.errors import ImportApplicationError
from umacircle_bot.sheets.legacy_room_ledger import (
    LegacyRoomLedgerReconciliation,
    LegacyRoomLedgerRow,
)
from umacircle_bot.sheets.row_parsers import RoomPointSourceRow


@dataclass(frozen=True)
class LegacyRoomImportResult:
    identity_rows_applied: int
    ledger_rows_applied: int
    game_account_ids: tuple[int, ...]
    final_point_total: int


def apply_legacy_room_identity_points(
    session: Session,
    *,
    import_run_id: int,
    source_identifier: str,
    point_rows: tuple[RoomPointSourceRow, ...],
    ledger_rows: tuple[LegacyRoomLedgerRow, ...],
    reconciliation: LegacyRoomLedgerReconciliation,
) -> NoReturn:
    """Fail closed: the retired combined writer persisted pre-scale Circle Point units."""
    del session, import_run_id, source_identifier, point_rows, ledger_rows, reconciliation
    raise ImportApplicationError(
        "retired legacy room writer is disabled; use the staged identity, race, ledger, and payout importers"
    )
