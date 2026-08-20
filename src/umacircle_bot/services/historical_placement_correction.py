from __future__ import annotations

from sqlalchemy.orm import Session

from umacircle_bot.services._historical_placement_manifest import (
    HISTORICAL_PLACEMENT_CORRECTION_IMPORT_KIND,
    HISTORICAL_PLACEMENT_CORRECTION_RECORD_TYPE,
    HISTORICAL_PLACEMENT_CORRECTION_SHEET_NAME,
    HISTORICAL_PLACEMENT_CORRECTION_SOURCE_TYPE,
    HISTORICAL_PLACEMENT_CORRECTION_VERSION,
    HISTORICAL_PLACEMENT_TARGET_EXTERNAL_IDS,
    HISTORICAL_PLACEMENT_TRANSACTION_SOURCE,
    HISTORICAL_PLACEMENT_TRANSACTION_TYPE,
    HistoricalPlacementConflict,
    HistoricalPlacementCorrectionApplyResult,
    HistoricalPlacementCorrectionManifest,
    HistoricalPlacementCorrectionPlan,
    HistoricalPlacementRaceManifestRow,
    HistoricalPlacementResultManifestRow,
)
from umacircle_bot.services._historical_placement_manifest import (
    build_historical_placement_correction_plan as _build_plan,
)
from umacircle_bot.services._historical_placement_persistence import (
    apply_historical_placement_correction as _apply_correction,
)
from umacircle_bot.services._historical_placement_prefix import _require_e2_prefix_runs
from umacircle_bot.services.legacy_identity_mapping import (
    load_applied_legacy_identity_mapping_audit,
)

__all__ = [
    "HISTORICAL_PLACEMENT_CORRECTION_IMPORT_KIND",
    "HISTORICAL_PLACEMENT_CORRECTION_RECORD_TYPE",
    "HISTORICAL_PLACEMENT_CORRECTION_SHEET_NAME",
    "HISTORICAL_PLACEMENT_CORRECTION_SOURCE_TYPE",
    "HISTORICAL_PLACEMENT_CORRECTION_VERSION",
    "HISTORICAL_PLACEMENT_TARGET_EXTERNAL_IDS",
    "HISTORICAL_PLACEMENT_TRANSACTION_SOURCE",
    "HISTORICAL_PLACEMENT_TRANSACTION_TYPE",
    "HistoricalPlacementConflict",
    "HistoricalPlacementCorrectionApplyResult",
    "HistoricalPlacementCorrectionManifest",
    "HistoricalPlacementCorrectionPlan",
    "HistoricalPlacementRaceManifestRow",
    "HistoricalPlacementResultManifestRow",
    "apply_historical_placement_correction",
    "build_historical_placement_correction_plan",
    "load_applied_legacy_identity_mapping_audit",
]


def build_historical_placement_correction_plan(
    session: Session,
    *,
    source_identifier: str,
    source_checksum: str,
    confirmed_mapping_decision_checksum: str,
    e2_business_key_prefix_checksum: str,
    lock_rows: bool = False,
) -> HistoricalPlacementCorrectionPlan:
    """Build the deterministic Race 52-56 correction manifest without writes."""

    return _build_plan(
        session,
        source_identifier=source_identifier,
        source_checksum=source_checksum,
        confirmed_mapping_decision_checksum=confirmed_mapping_decision_checksum,
        e2_business_key_prefix_checksum=e2_business_key_prefix_checksum,
        mapping_loader=load_applied_legacy_identity_mapping_audit,
        prefix_runs_verifier=_require_e2_prefix_runs,
        lock_rows=lock_rows,
    )


def apply_historical_placement_correction(
    session: Session,
    *,
    source_identifier: str,
    source_checksum: str,
    confirmed_mapping_decision_checksum: str,
    e2_business_key_prefix_checksum: str,
    confirmed_manifest_checksum: str,
) -> HistoricalPlacementCorrectionApplyResult:
    """Append the reviewed correction or validate an exact retry."""

    return _apply_correction(
        session,
        source_identifier=source_identifier,
        source_checksum=source_checksum,
        confirmed_mapping_decision_checksum=confirmed_mapping_decision_checksum,
        e2_business_key_prefix_checksum=e2_business_key_prefix_checksum,
        confirmed_manifest_checksum=confirmed_manifest_checksum,
        build_plan=build_historical_placement_correction_plan,
    )
