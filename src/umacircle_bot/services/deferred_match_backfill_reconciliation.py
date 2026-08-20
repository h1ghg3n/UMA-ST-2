from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import CirclePointAccount
from umacircle_bot.domain.errors import LegacyImportError
from umacircle_bot.domain.imports import normalize_import_source_identifier, normalize_sha256_hex
from umacircle_bot.services.current_win5_replay_provenance import (
    inspect_current_win5_replay_provenance,
    load_current_win5_replay_runs,
)
from umacircle_bot.services.current_win5_replay_state import (
    build_current_win5_replay_final_report,
    build_current_win5_replay_participant_checksum,
)
from umacircle_bot.services.deferred_match_backfill_verification import (
    build_backfill_population_report,
    build_completed_backfill_report,
)
from umacircle_bot.services.deferred_match_reconciliation_signatures import (
    DEFERRED_MATCH_RECONCILIATION_VERSION,
    build_circle_point_signature_payload,
    build_payload_signature,
    build_win5_signature_payload,
)
from umacircle_bot.services.legacy_race_import import LegacyRaceImportPlan
from umacircle_bot.services.legacy_result_import import (
    LEGACY_RESULT_IMPORT_KIND,
    LEGACY_ROOM_RESULT_IMPORT_MANIFEST,
    LegacyRoomResultPersistencePlan,
)
from umacircle_bot.services.mariadb_consistent_read import (
    open_mariadb_consistent_read_session,
    require_mariadb_consistent_read,
)
from umacircle_bot.sheets.current_win5_replay_manifest import CurrentWin5ReplayManifest

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SQLITE_TEST_CONSISTENT_READ_TOKEN = object()
_SQLITE_TEST_CONSISTENT_READ_KEY = "deferred_match_test_consistent_read"


@dataclass(frozen=True, slots=True)
class DeferredMatchBackfillReconciliation:
    signature_version: int
    writes_quiesced: bool
    consistent_read: bool
    mode: str
    source_identifier: str
    source_checksum: str
    baseline_checksum: str
    replay_manifest_checksum: str
    replay_file_checksum: str
    replay_bot_commit: str
    backfill_bot_commit: str
    replay_import_run_id: int | None
    season_id: int | None
    round_id: int | None
    circle_point_signature: str
    win5_signature: str
    circle_point: dict[str, object]
    win5: dict[str, object]
    backfill: dict[str, object]
    errors: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), "ready": self.ready}


def build_deferred_match_backfill_reconciliation(
    session: Session,
    *,
    manifest: CurrentWin5ReplayManifest,
    replay_bot_commit: str,
    backfill_bot_commit: str,
    runtime_backfill_bot_commit: str,
    writes_quiesced: bool,
    source_race_plan: LegacyRaceImportPlan | None = None,
    source_result_plan: LegacyRoomResultPersistencePlan | None = None,
    expected_circle_point_signature: str | None = None,
    expected_win5_signature: str | None = None,
    confirmed_mapping_decision_checksum: str | None = None,
    confirmed_disposition_decision_checksum: str | None = None,
    rating_rule_version_id: int | None = None,
    result_import_kind: str = LEGACY_RESULT_IMPORT_KIND,
) -> DeferredMatchBackfillReconciliation:
    """Build the before/after S5 reconciliation without mutating the database.

    Snapshot mode freezes the replay-era monetary and WIN5 graph. Complete mode
    additionally proves that the reviewed deferred Match import/mapping/Rating
    chain exists while those two row-level signatures remain unchanged.
    """

    _require_clean_session(session)
    if writes_quiesced is not True:
        raise LegacyImportError("deferred Match reconciliation requires confirmed write quiescence")
    _require_physical_consistent_read(session)
    source_identifier = normalize_import_source_identifier(manifest.room_match_source_identifier)
    source_checksum = normalize_sha256_hex(manifest.room_match_source_checksum, field_name="source checksum")
    normalized_replay_commit = _normalize_commit(replay_bot_commit, field="replay bot commit")
    normalized_backfill_commit = _normalize_commit(backfill_bot_commit, field="backfill bot commit")
    normalized_runtime_backfill_commit = _normalize_commit(
        runtime_backfill_bot_commit,
        field="runtime backfill bot commit",
    )

    completion_values = (
        expected_circle_point_signature,
        expected_win5_signature,
        confirmed_mapping_decision_checksum,
        confirmed_disposition_decision_checksum,
        rating_rule_version_id,
        source_race_plan,
        source_result_plan,
    )
    completion_mode = any(value is not None for value in completion_values)
    if completion_mode and any(value is None for value in completion_values):
        raise LegacyImportError("complete reconciliation requires every before-signature and reviewed backfill input")

    errors: list[str] = []
    if normalized_backfill_commit != normalized_runtime_backfill_commit:
        errors.append("backfill_bot_commit_mismatch")
    canonical_report, canonical_errors = build_current_win5_replay_final_report(
        session,
        manifest=manifest,
    )
    errors.extend(canonical_errors)
    point_report, point_payload = _build_circle_point_report(session, canonical_report=canonical_report)
    win5_report, win5_payload, replay_import_run_id, win5_errors = _build_win5_report(
        session,
        manifest=manifest,
        replay_bot_commit=normalized_replay_commit,
        canonical_report=canonical_report,
    )
    errors.extend(win5_errors)
    circle_point_signature = build_payload_signature("circle_point", point_payload)
    win5_signature = build_payload_signature("win5", win5_payload)

    backfill_report: dict[str, object] = build_backfill_population_report(
        session,
        source_identifier=source_identifier,
        result_import_kind=result_import_kind,
    )
    if completion_mode:
        expected_point_signature = normalize_sha256_hex(
            expected_circle_point_signature,
            field_name="expected Circle Point signature",
        )
        expected_replay_signature = normalize_sha256_hex(
            expected_win5_signature,
            field_name="expected WIN5 signature",
        )
        mapping_checksum = normalize_sha256_hex(
            confirmed_mapping_decision_checksum,
            field_name="confirmed mapping decision checksum",
        )
        disposition_checksum = normalize_sha256_hex(
            confirmed_disposition_decision_checksum,
            field_name="confirmed disposition decision checksum",
        )
        rating_rule_version_id = _positive_int(rating_rule_version_id, field="RatingRuleVersion ID")
        if circle_point_signature != expected_point_signature:
            errors.append("circle_point_signature_changed")
        if win5_signature != expected_replay_signature:
            errors.append("win5_signature_changed")
        completed_report, completion_errors = build_completed_backfill_report(
            session,
            source_identifier=source_identifier,
            source_checksum=source_checksum,
            confirmed_mapping_decision_checksum=mapping_checksum,
            confirmed_disposition_decision_checksum=disposition_checksum,
            rating_rule_version_id=rating_rule_version_id,
            result_import_kind=result_import_kind,
            expected_race_count=LEGACY_ROOM_RESULT_IMPORT_MANIFEST.race_count,
            expected_entry_count=LEGACY_ROOM_RESULT_IMPORT_MANIFEST.row_count,
            expected_result_count=LEGACY_ROOM_RESULT_IMPORT_MANIFEST.row_count,
            source_race_plan=source_race_plan,
            source_result_plan=source_result_plan,
        )
        backfill_report = completed_report
        errors.extend(completion_errors)

    return DeferredMatchBackfillReconciliation(
        signature_version=DEFERRED_MATCH_RECONCILIATION_VERSION,
        writes_quiesced=True,
        consistent_read=True,
        mode="verify_complete" if completion_mode else "snapshot",
        source_identifier=source_identifier,
        source_checksum=source_checksum,
        baseline_checksum=manifest.win5_first_baseline_checksum,
        replay_manifest_checksum=manifest.manifest_checksum,
        replay_file_checksum=manifest.file_checksum,
        replay_bot_commit=normalized_replay_commit,
        backfill_bot_commit=normalized_backfill_commit,
        replay_import_run_id=replay_import_run_id,
        season_id=_optional_int(win5_report.get("season_id")),
        round_id=_optional_int(win5_report.get("round_id")),
        circle_point_signature=circle_point_signature,
        win5_signature=win5_signature,
        circle_point=point_report,
        win5=win5_report,
        backfill=backfill_report,
        errors=tuple(dict.fromkeys(errors)),
    )


@contextmanager
def open_deferred_match_reconciliation_session(*, engine: Engine | None = None) -> Iterator[Session]:
    """Open one physically read-only MariaDB repeatable-read snapshot."""

    with open_mariadb_consistent_read_session(engine=engine) as session:
        yield session


@contextmanager
def _allow_sqlite_test_consistent_read(session: Session) -> Iterator[None]:
    """Allow fast tests to exercise pure reconciliation after the public MariaDB gate."""

    if session.get_bind().dialect.name != "sqlite":
        raise LegacyImportError("SQLite consistent-read bypass is test-only")
    previous = session.info.get(_SQLITE_TEST_CONSISTENT_READ_KEY)
    session.info[_SQLITE_TEST_CONSISTENT_READ_KEY] = _SQLITE_TEST_CONSISTENT_READ_TOKEN
    try:
        yield
    finally:
        if previous is None:
            session.info.pop(_SQLITE_TEST_CONSISTENT_READ_KEY, None)
        else:
            session.info[_SQLITE_TEST_CONSISTENT_READ_KEY] = previous


def _require_physical_consistent_read(session: Session) -> None:
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "sqlite":
        if session.info.get(_SQLITE_TEST_CONSISTENT_READ_KEY) is _SQLITE_TEST_CONSISTENT_READ_TOKEN:
            return
        raise LegacyImportError("deferred Match reconciliation requires MariaDB")
    try:
        require_mariadb_consistent_read(session)
    except LegacyImportError as exc:
        raise LegacyImportError("deferred Match reconciliation requires physical read-only repeatable-read") from exc


def _build_circle_point_report(
    session: Session,
    *,
    canonical_report: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    canonical_point_report = canonical_report.get("room_points")
    report = dict(canonical_point_report) if isinstance(canonical_point_report, dict) else {}
    report["wallet_count"] = _count(session, CirclePointAccount)
    return report, build_circle_point_signature_payload(session)


def _build_win5_report(
    session: Session,
    *,
    manifest: CurrentWin5ReplayManifest,
    replay_bot_commit: str,
    canonical_report: dict[str, object],
) -> tuple[dict[str, object], dict[str, object], int | None, tuple[str, ...]]:
    participant_checksum = build_current_win5_replay_participant_checksum(manifest.participant_discord_user_ids)
    provenance_errors, replay_run = inspect_current_win5_replay_provenance(
        session,
        runs=load_current_win5_replay_runs(session, lock=False),
        manifest=manifest,
        participant_checksum=participant_checksum,
        final_report=canonical_report,
        bot_commit=replay_bot_commit,
        lock=False,
    )
    payload, participant_count = build_win5_signature_payload(session, manifest=manifest)
    report = {
        **canonical_report,
        "participant_count": participant_count,
        "participant_checksum": participant_checksum,
    }
    return report, payload, replay_run.id if replay_run is not None else None, provenance_errors


def _count(session: Session, model: type[object]) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _normalize_commit(value: object, *, field: str) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if not _COMMIT_RE.fullmatch(normalized):
        raise LegacyImportError(f"{field} must be a full 40-character Git SHA")
    return normalized


def _positive_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise LegacyImportError(f"{field} must be a positive integer")
    return value


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _require_clean_session(session: Session) -> None:
    if session.new or session.dirty or session.deleted:
        raise LegacyImportError("deferred Match reconciliation requires a clean session")
