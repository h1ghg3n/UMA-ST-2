from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    DiscordAccount,
    GameAccount,
    Persona,
    Race,
    RaceCondition,
    RaceEntry,
    RaceRatingContext,
    RaceResult,
    RatingEvent,
    SheetImportRecord,
    SheetImportRun,
)
from umacircle_bot.domain.errors import LegacyImportError
from umacircle_bot.domain.imports import normalize_import_source_identifier, normalize_sha256_hex
from umacircle_bot.domain.player_link import normalize_player_name_strict
from umacircle_bot.services.legacy_import import LEGACY_IDENTITY_POINT_RECORD_TYPE
from umacircle_bot.services.legacy_rating_chain import (
    RATING_REPLAY_MODES,
    RATING_REPLAY_RECALCULATE,
    RATING_REPLAY_SOURCE_EXACT,
    build_legacy_rating_chain_evidence,
    empty_legacy_rating_chain_evidence,
    validate_legacy_rating_replay_chains,
)
from umacircle_bot.services.legacy_result_import import (
    LEGACY_RESULT_IMPORT_KIND,
    LEGACY_RESULT_RECORD_TYPE,
)

MAPPING_MANIFEST_VERSION = 3
LEGACY_IDENTITY_MAPPING_IMPORT_KIND = "legacy_room_identity_mapping"
LEGACY_IDENTITY_MAPPING_RECORD_TYPE = "legacy_identity_mapping"
LEGACY_IDENTITY_MAPPING_SOURCE_TYPE = "operator_manifest"
LEGACY_IDENTITY_MAPPING_SHEET_NAME = "identity_mapping"
RATING_REPLAY = "rating_replay"
HISTORY_CONTEXT_ONLY = "history_context_only"
UNRESOLVED_HISTORY = "unresolved_history"
LEGACY_IDENTITY_DISPOSITIONS = frozenset(
    {
        RATING_REPLAY,
        HISTORY_CONTEXT_ONLY,
        UNRESOLVED_HISTORY,
    }
)


@dataclass(frozen=True, slots=True)
class LegacyIdentityMappingPreview:
    source_identifier: str
    source_checksum: str
    mapping_checksum: str
    decision_checksum: str
    mapping_count: int
    occurrence_count: int
    rating_replay_count: int
    history_context_only_count: int
    unresolved_history_count: int
    already_applied: bool


@dataclass(frozen=True, slots=True)
class LegacyIdentityMappingApplyResult:
    import_run_id: int
    source_identifier: str
    source_checksum: str
    mapping_checksum: str
    decision_checksum: str
    mapping_count: int
    mapped_occurrence_count: int
    skipped_occurrence_count: int
    history_context_only_occurrence_count: int
    unresolved_history_occurrence_count: int


@dataclass(frozen=True, slots=True)
class LegacyIdentityMappingDecision:
    normalized_player_name: str
    source_chain_key: str
    source_record_keys: tuple[str, ...]
    game_account_id: int | None
    persona_id: str | None
    disposition: str
    rating_replay_mode: str | None
    relationship_snapshot: dict[str, object] | None
    operator_note: str | None


@dataclass(frozen=True, slots=True)
class AppliedLegacyIdentityMappingAudit:
    import_run_id: int
    source_identifier: str
    source_checksum: str
    mapping_checksum: str
    decision_checksum: str
    decision_manifest: dict[str, object]
    decisions: tuple[LegacyIdentityMappingDecision, ...]


@dataclass(frozen=True, slots=True)
class _ApprovedIdentityMapping:
    normalized_player_name: str
    game_account_id: int | None
    persona_id: str | None
    disposition: str
    rating_replay_mode: str | None
    operator_note: str | None
    relationship_snapshot: dict[str, object] | None
    source_player_names: tuple[str, ...]
    source_record_keys: tuple[str, ...]
    occurrence_count: int


@dataclass(frozen=True, slots=True)
class _IdentityMappingDecisionPlan:
    source_identifier: str
    source_checksum: str
    mapping_checksum: str
    decision_checksum: str
    reviewed_by: tuple[str, ...]
    reviewed_at: str
    evidence_note: str | None
    mappings: tuple[_ApprovedIdentityMapping, ...]

    @property
    def disposition_counts(self) -> Counter[str]:
        return Counter(mapping.disposition for mapping in self.mappings)


def build_legacy_identity_mapping_manifest(
    session: Session,
    *,
    source_identifier: str,
    source_checksum: str,
) -> dict[str, object]:
    """Build a deterministic, read-only operator review template for legacy names.

    Exact normalized-name matches are suggestions only. This function never writes
    an identity decision or changes imported RaceEntry/RaceResult ownership.
    """

    normalized_source = normalize_import_source_identifier(source_identifier)
    normalized_checksum = normalize_sha256_hex(source_checksum, field_name="source checksum")
    if session.new or session.dirty or session.deleted:
        raise LegacyImportError("legacy identity mapping manifest requires a clean session")

    result_runs = tuple(
        session.scalars(
            select(SheetImportRun).where(
                SheetImportRun.import_kind == LEGACY_RESULT_IMPORT_KIND,
                SheetImportRun.source_type == "xlsx",
                SheetImportRun.source_identifier == normalized_source,
                SheetImportRun.source_checksum == normalized_checksum,
                SheetImportRun.status == "completed",
                SheetImportRun.finished_at.is_not(None),
            )
        )
    )
    if len(result_runs) != 1:
        raise LegacyImportError("legacy identity mapping requires one confirmed result import run")
    result_run = result_runs[0]

    source_entry_count = int(
        session.scalar(
            select(func.count())
            .select_from(RaceEntry)
            .join(Race, Race.id == RaceEntry.race_id)
            .where(Race.external_source == normalized_source, Race.race_kind == "room_match")
        )
        or 0
    )
    source_result_count = int(
        session.scalar(
            select(func.count())
            .select_from(RaceResult)
            .join(Race, Race.id == RaceResult.race_id)
            .where(Race.external_source == normalized_source, Race.race_kind == "room_match")
        )
        or 0
    )
    source_rows = tuple(
        session.execute(
            select(RaceEntry, RaceResult, RaceCondition.grade)
            .join(Race, Race.id == RaceEntry.race_id)
            .join(
                RaceResult,
                and_(
                    RaceResult.race_id == RaceEntry.race_id,
                    RaceResult.entry_number == RaceEntry.entry_number,
                ),
            )
            .join(RaceCondition, RaceCondition.race_id == Race.id)
            .where(Race.external_source == normalized_source, Race.race_kind == "room_match")
            .order_by(Race.starts_at, Race.id, RaceEntry.entry_number)
        )
    )
    if not source_rows or source_entry_count != source_result_count or len(source_rows) != source_entry_count:
        raise LegacyImportError("legacy identity mapping requires one result for every imported entry")
    result_records = _validate_result_provenance(
        source_rows=source_rows,
        result_run_id=result_run.id,
        session=session,
    )

    accounts = tuple(session.scalars(select(GameAccount).order_by(GameAccount.id)))
    personas = {persona.id: persona for persona in session.scalars(select(Persona))}
    discord_accounts = tuple(session.scalars(select(DiscordAccount).order_by(DiscordAccount.id)))
    discord_by_id = {account.id: account for account in discord_accounts}
    discord_by_persona: dict[str, list[DiscordAccount]] = defaultdict(list)
    for discord in discord_accounts:
        if discord.persona_id is not None:
            discord_by_persona[discord.persona_id].append(discord)
    peer_account_ids_by_persona: dict[str, list[int]] = defaultdict(list)
    for account in accounts:
        if account.persona_id is not None:
            peer_account_ids_by_persona[account.persona_id].append(account.id)
    identity_source_keys = _identity_source_keys(
        session,
        source_identifier=normalized_source,
        source_checksum=normalized_checksum,
    )
    rating_chain_evidence = build_legacy_rating_chain_evidence(
        session,
        source_identifier=normalized_source,
    )

    account_details: dict[int, dict[str, object]] = {}
    exact_candidates: dict[str, set[int]] = defaultdict(set)
    for account in accounts:
        persona = personas.get(account.persona_id) if account.persona_id is not None else None
        names = {account.nickname, account.ingame_name, persona.display_name if persona is not None else None}
        if account.discord_account_id is not None and account.discord_account_id in discord_by_id:
            names.add(discord_by_id[account.discord_account_id].discord_nickname)
        if account.persona_id is not None:
            names.update(discord.discord_nickname for discord in discord_by_persona[account.persona_id])
        for name in names:
            if isinstance(name, str) and normalize_player_name_strict(name):
                exact_candidates[normalize_player_name_strict(name)].add(account.id)
        account_details[account.id] = {
            "game_account_id": account.id,
            "persona_id": account.persona_id,
            "identity_status": account.identity_status,
            "nickname": account.nickname,
            "ingame_name": account.ingame_name,
            "identity_source_keys": tuple(identity_source_keys.get(account.id, ())),
            "persona_peer_game_account_ids": tuple(sorted(peer_account_ids_by_persona.get(account.persona_id, ()))),
            "is_source_only": account.persona_id is None,
        }

    rows_by_name: dict[str, list[tuple[RaceEntry, RaceResult, str]]] = defaultdict(list)
    source_names: dict[str, set[str]] = defaultdict(set)
    for entry, result, grade in source_rows:
        if not isinstance(entry.player_name, str) or not entry.player_name.strip():
            raise LegacyImportError("legacy identity mapping source contains an empty player name")
        normalized_name = normalize_player_name_strict(entry.player_name)
        if not normalized_name:
            raise LegacyImportError("legacy identity mapping source contains an empty normalized player name")
        rows_by_name[normalized_name].append((entry, result, grade))
        source_names[normalized_name].add(entry.player_name)

    mappings: list[dict[str, object]] = []
    status_counts: Counter[str] = Counter()
    for normalized_name in sorted(rows_by_name):
        rows = rows_by_name[normalized_name]
        current_account_ids: set[int] = set()
        unmapped_occurrence_count = 0
        inconsistent_occurrence_count = 0
        for entry, result, _grade in rows:
            if entry.game_account_id is None and result.game_account_id is None:
                unmapped_occurrence_count += 1
            elif entry.game_account_id is not None and entry.game_account_id == result.game_account_id:
                current_account_ids.add(entry.game_account_id)
            else:
                inconsistent_occurrence_count += 1

        candidate_ids = tuple(sorted(exact_candidates.get(normalized_name, ())))
        current_ids = tuple(sorted(current_account_ids))
        status, suggested_account_id, approved_account_id = _mapping_status(
            current_account_ids=current_ids,
            candidate_account_ids=candidate_ids,
            unmapped_occurrence_count=unmapped_occurrence_count,
            inconsistent_occurrence_count=inconsistent_occurrence_count,
        )
        status_counts[status] += 1
        candidate_details = tuple(account_details[account_id] for account_id in candidate_ids)
        approved_persona_id = (
            account_details[approved_account_id]["persona_id"] if approved_account_id is not None else None
        )
        mappings.append(
            {
                "normalized_player_name": normalized_name,
                "source_chain_key": _source_chain_key(
                    source_identifier=normalized_source,
                    normalized_player_name=normalized_name,
                ),
                "source_player_names": tuple(sorted(source_names[normalized_name])),
                "source_record_keys": tuple(
                    sorted(result_records[entry.source_import_record_id].source_key for entry, _result, _grade in rows)
                ),
                "occurrence_count": len(rows),
                "race_count": len({entry.race_id for entry, _result, _grade in rows}),
                "rating_eligible_occurrence_count": sum(
                    grade.upper() != "OP" and not result.is_rating_excluded and not result.is_result_void
                    for _entry, result, grade in rows
                ),
                "current_game_account_ids": current_ids,
                "unmapped_occurrence_count": unmapped_occurrence_count,
                "inconsistent_occurrence_count": inconsistent_occurrence_count,
                "exact_candidate_game_account_ids": candidate_ids,
                "exact_candidates": candidate_details,
                "rating_chain": rating_chain_evidence.get(
                    normalized_name,
                    empty_legacy_rating_chain_evidence(),
                ),
                "status": status,
                "suggested_game_account_id": suggested_account_id,
                "decision": {
                    "approved_game_account_id": approved_account_id,
                    "approved_persona_id": approved_persona_id,
                    "disposition": RATING_REPLAY if approved_account_id is not None else None,
                    "rating_replay_mode": (RATING_REPLAY_SOURCE_EXACT if approved_account_id is not None else None),
                    "operator_note": None,
                },
            }
        )

    manifest_payload = {
        "manifest_version": MAPPING_MANIFEST_VERSION,
        "source_identifier": normalized_source,
        "source_checksum": normalized_checksum,
        "result_import_run_id": result_run.id,
        "game_accounts": tuple(account_details[account_id] for account_id in sorted(account_details)),
        "mappings": mappings,
    }
    mapping_basis = {
        "manifest_version": MAPPING_MANIFEST_VERSION,
        "source_identifier": normalized_source,
        "source_checksum": normalized_checksum,
        "game_accounts": manifest_payload["game_accounts"],
        "mappings": [
            {
                "normalized_player_name": mapping["normalized_player_name"],
                "source_chain_key": mapping["source_chain_key"],
                "source_player_names": mapping["source_player_names"],
                "source_record_keys": mapping["source_record_keys"],
                "occurrence_count": mapping["occurrence_count"],
                "race_count": mapping["race_count"],
                "rating_eligible_occurrence_count": mapping["rating_eligible_occurrence_count"],
                "exact_candidate_game_account_ids": mapping["exact_candidate_game_account_ids"],
                "exact_candidates": mapping["exact_candidates"],
                "rating_chain": mapping["rating_chain"],
            }
            for mapping in mappings
        ],
    }
    mapping_checksum = sha256(
        json.dumps(mapping_basis, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        **manifest_payload,
        "generated_at": datetime.now(UTC).isoformat(),
        "mapping_checksum": mapping_checksum,
        "mode": "review_template",
        "writes_database": False,
        "review": {
            "reviewed_by": (),
            "reviewed_at": None,
            "evidence_note": None,
        },
        "summary": {
            "source_entry_count": source_entry_count,
            "source_result_count": source_result_count,
            "normalized_player_count": len(mappings),
            "status_counts": dict(sorted(status_counts.items())),
            "operator_decision_count": sum(mapping["status"] != "already_mapped" for mapping in mappings),
        },
    }


def preview_legacy_identity_mapping(
    session: Session,
    *,
    decision_manifest: Mapping[str, object],
) -> LegacyIdentityMappingPreview:
    """Validate a completed operator decision artifact without writing the DB."""

    _require_clean_session(session)
    plan = _build_decision_plan(session, decision_manifest=decision_manifest)
    runs = _mapping_import_runs(session, source_identifier=plan.source_identifier)
    if len(runs) > 1:
        raise LegacyImportError("legacy identity mapping has multiple completed apply runs")
    already_applied = bool(runs)
    occurrence_count = _validate_current_source_owners(
        session,
        plan=plan,
        require_applied_snapshots=already_applied,
    )
    if already_applied:
        run = runs[0]
        if run.source_checksum != plan.decision_checksum:
            raise LegacyImportError("legacy identity mapping was already applied with a different decision")
        _validate_mapping_audit(session, run=run, plan=plan)
    disposition_counts = plan.disposition_counts
    return LegacyIdentityMappingPreview(
        source_identifier=plan.source_identifier,
        source_checksum=plan.source_checksum,
        mapping_checksum=plan.mapping_checksum,
        decision_checksum=plan.decision_checksum,
        mapping_count=len(plan.mappings),
        occurrence_count=occurrence_count,
        rating_replay_count=disposition_counts[RATING_REPLAY],
        history_context_only_count=disposition_counts[HISTORY_CONTEXT_ONLY],
        unresolved_history_count=disposition_counts[UNRESOLVED_HISTORY],
        already_applied=already_applied,
    )


def apply_legacy_identity_mapping(
    session: Session,
    *,
    decision_manifest: Mapping[str, object],
    confirmed_source_checksum: str,
    confirmed_decision_checksum: str,
) -> LegacyIdentityMappingApplyResult:
    """Atomically apply a two-reviewer legacy name decision to Entry and Result."""

    _require_clean_session(session)
    normalized_source_confirmation = normalize_sha256_hex(
        confirmed_source_checksum,
        field_name="confirmed source checksum",
    )
    normalized_decision_confirmation = normalize_sha256_hex(
        confirmed_decision_checksum,
        field_name="confirmed decision checksum",
    )
    with session.begin_nested():
        _lock_identity_mapping_scope(session, decision_manifest=decision_manifest)
        if session.scalar(select(RatingEvent.id).limit(1).with_for_update()) is not None:
            raise LegacyImportError("legacy identity mapping must be applied before Rating events exist")
        if session.scalar(select(RaceRatingContext.id).limit(1).with_for_update()) is not None:
            raise LegacyImportError("legacy identity mapping must be applied before Rating contexts exist")

        plan = _build_decision_plan(session, decision_manifest=decision_manifest)
        if plan.source_checksum != normalized_source_confirmation:
            raise LegacyImportError("confirmed source checksum does not match the mapping decision")
        if plan.decision_checksum != normalized_decision_confirmation:
            raise LegacyImportError("confirmed decision checksum does not match the mapping decision")
        runs = _mapping_import_runs(session, source_identifier=plan.source_identifier, lock_rows=True)
        if len(runs) > 1:
            raise LegacyImportError("legacy identity mapping has multiple completed apply runs")
        occurrence_count = _validate_current_source_owners(
            session,
            plan=plan,
            require_applied_snapshots=bool(runs),
        )
        if runs:
            run = runs[0]
            if run.source_checksum != plan.decision_checksum:
                raise LegacyImportError("legacy identity mapping was already applied with a different decision")
            _validate_mapping_audit(session, run=run, plan=plan)
            return LegacyIdentityMappingApplyResult(
                import_run_id=run.id,
                source_identifier=plan.source_identifier,
                source_checksum=plan.source_checksum,
                mapping_checksum=plan.mapping_checksum,
                decision_checksum=plan.decision_checksum,
                mapping_count=len(plan.mappings),
                mapped_occurrence_count=0,
                skipped_occurrence_count=_disposition_occurrence_count(plan, RATING_REPLAY),
                history_context_only_occurrence_count=_disposition_occurrence_count(
                    plan,
                    HISTORY_CONTEXT_ONLY,
                ),
                unresolved_history_occurrence_count=_disposition_occurrence_count(
                    plan,
                    UNRESOLVED_HISTORY,
                ),
            )

        approved_by_name = {mapping.normalized_player_name: mapping for mapping in plan.mappings}
        source_rows = _source_identity_rows(session, source_identifier=plan.source_identifier)
        mapped_occurrence_count = 0
        skipped_occurrence_count = 0
        history_context_only_occurrence_count = 0
        unresolved_history_occurrence_count = 0
        affected_counts: Counter[str] = Counter()
        for entry, result, _grade in source_rows:
            normalized_name = normalize_player_name_strict(entry.player_name or "")
            approved = approved_by_name[normalized_name]
            if approved.disposition == RATING_REPLAY:
                assert approved.game_account_id is not None
                if entry.game_account_id is None and result.game_account_id is None:
                    entry.game_account_id = approved.game_account_id
                    result.game_account_id = approved.game_account_id
                    mapped_occurrence_count += 1
                elif entry.game_account_id == result.game_account_id == approved.game_account_id:
                    skipped_occurrence_count += 1
                else:
                    raise LegacyImportError("legacy identity mapping source owner changed after review")
                entry.owner_at_event_persona_id = approved.persona_id
                result.owner_at_event_persona_id = approved.persona_id
            elif entry.game_account_id is not None or result.game_account_id is not None:
                raise LegacyImportError("legacy identity mapping non-replay source owner changed after review")
            elif approved.disposition == HISTORY_CONTEXT_ONLY:
                history_context_only_occurrence_count += 1
            elif approved.disposition == UNRESOLVED_HISTORY:
                unresolved_history_occurrence_count += 1
            else:
                raise AssertionError("unsupported legacy identity mapping disposition")
            affected_counts[normalized_name] += 1

        run = SheetImportRun(
            import_kind=LEGACY_IDENTITY_MAPPING_IMPORT_KIND,
            source_type=LEGACY_IDENTITY_MAPPING_SOURCE_TYPE,
            source_identifier=plan.source_identifier,
            source_checksum=plan.decision_checksum,
            status="running",
        )
        session.add(run)
        session.flush()
        for row_number, approved in enumerate(plan.mappings, start=1):
            target_entity_type = "game_account" if approved.game_account_id is not None else None
            session.add(
                SheetImportRecord(
                    import_run_id=run.id,
                    source_key=_mapping_record_source_key(
                        source_identifier=plan.source_identifier,
                        decision_checksum=plan.decision_checksum,
                        normalized_player_name=approved.normalized_player_name,
                    ),
                    row_fingerprint=_mapping_record_fingerprint(approved),
                    source_sheet_name=LEGACY_IDENTITY_MAPPING_SHEET_NAME,
                    source_row_number=row_number,
                    record_type=LEGACY_IDENTITY_MAPPING_RECORD_TYPE,
                    status="applied",
                    target_entity_type=target_entity_type,
                    target_entity_id=approved.game_account_id,
                    detail_json={
                        "mapping_manifest_version": MAPPING_MANIFEST_VERSION,
                        "source_checksum": plan.source_checksum,
                        "mapping_checksum": plan.mapping_checksum,
                        "decision_checksum": plan.decision_checksum,
                        "normalized_player_name": approved.normalized_player_name,
                        "source_chain_key": _source_chain_key(
                            source_identifier=plan.source_identifier,
                            normalized_player_name=approved.normalized_player_name,
                        ),
                        "source_player_names": list(approved.source_player_names),
                        "source_record_keys": list(approved.source_record_keys),
                        "affected_occurrence_count": affected_counts[approved.normalized_player_name],
                        "approved_game_account_id": approved.game_account_id,
                        "approved_persona_id": approved.persona_id,
                        "disposition": approved.disposition,
                        "rating_replay_mode": approved.rating_replay_mode,
                        "relationship_snapshot": approved.relationship_snapshot,
                        "operator_note": approved.operator_note,
                    },
                )
            )
        run.status = "completed"
        run.finished_at = datetime.now(UTC)
        run.summary_json = {
            "mapping_manifest_version": MAPPING_MANIFEST_VERSION,
            "source_checksum": plan.source_checksum,
            "mapping_checksum": plan.mapping_checksum,
            "decision_checksum": plan.decision_checksum,
            "mapping_count": len(plan.mappings),
            "occurrence_count": occurrence_count,
            "mapped_occurrence_count": mapped_occurrence_count,
            "skipped_occurrence_count": skipped_occurrence_count,
            "rating_replay_count": plan.disposition_counts[RATING_REPLAY],
            "history_context_only_count": plan.disposition_counts[HISTORY_CONTEXT_ONLY],
            "unresolved_history_count": plan.disposition_counts[UNRESOLVED_HISTORY],
            "history_context_only_occurrence_count": history_context_only_occurrence_count,
            "unresolved_history_occurrence_count": unresolved_history_occurrence_count,
            "reviewed_by": list(plan.reviewed_by),
            "reviewed_at": plan.reviewed_at,
            "evidence_note": plan.evidence_note,
        }
        session.flush()

    return LegacyIdentityMappingApplyResult(
        import_run_id=run.id,
        source_identifier=plan.source_identifier,
        source_checksum=plan.source_checksum,
        mapping_checksum=plan.mapping_checksum,
        decision_checksum=plan.decision_checksum,
        mapping_count=len(plan.mappings),
        mapped_occurrence_count=mapped_occurrence_count,
        skipped_occurrence_count=skipped_occurrence_count,
        history_context_only_occurrence_count=history_context_only_occurrence_count,
        unresolved_history_occurrence_count=unresolved_history_occurrence_count,
    )


def load_applied_legacy_identity_mapping_audit(
    session: Session,
    *,
    source_identifier: str,
    source_checksum: str,
    confirmed_decision_checksum: str | None = None,
    lock_rows: bool = False,
) -> AppliedLegacyIdentityMappingAudit:
    """Load and revalidate the reviewed S2 ownership decision without S3."""

    _require_clean_session(session)
    source = normalize_import_source_identifier(source_identifier)
    checksum = normalize_sha256_hex(source_checksum, field_name="source checksum")
    confirmation = (
        normalize_sha256_hex(
            confirmed_decision_checksum,
            field_name="confirmed mapping decision checksum",
        )
        if confirmed_decision_checksum is not None
        else None
    )
    runs = _mapping_import_runs(session, source_identifier=source, lock_rows=lock_rows)
    if len(runs) != 1:
        raise LegacyImportError("applied legacy identity mapping requires one completed audit")
    run = runs[0]
    summary = run.summary_json if isinstance(run.summary_json, dict) else {}
    if (
        summary.get("mapping_manifest_version") != MAPPING_MANIFEST_VERSION
        or summary.get("source_checksum") != checksum
    ):
        raise LegacyImportError("applied legacy identity mapping audit basis changed")

    statement = (
        select(SheetImportRecord)
        .where(SheetImportRecord.import_run_id == run.id)
        .order_by(SheetImportRecord.source_row_number)
    )
    if lock_rows:
        statement = statement.with_for_update()
    records = tuple(session.scalars(statement))
    decisions: dict[str, LegacyIdentityMappingDecision] = {}
    submitted_by_name: dict[str, dict[str, object]] = {}
    for record in records:
        detail = record.detail_json if isinstance(record.detail_json, dict) else {}
        name = detail.get("normalized_player_name")
        source_keys = detail.get("source_record_keys")
        disposition = detail.get("disposition")
        if (
            record.record_type != LEGACY_IDENTITY_MAPPING_RECORD_TYPE
            or record.status != "applied"
            or not isinstance(name, str)
            or not name
            or name in decisions
            or not isinstance(source_keys, (list, tuple))
            or not source_keys
            or not all(isinstance(key, str) and key for key in source_keys)
            or disposition not in LEGACY_IDENTITY_DISPOSITIONS
            or detail.get("source_checksum") != checksum
            or detail.get("mapping_checksum") != summary.get("mapping_checksum")
            or detail.get("decision_checksum") != summary.get("decision_checksum")
        ):
            raise LegacyImportError("applied legacy identity mapping audit is malformed")
        rating_replay_mode = detail.get("rating_replay_mode")
        if disposition == RATING_REPLAY:
            if rating_replay_mode not in RATING_REPLAY_MODES:
                raise LegacyImportError("applied legacy identity mapping replay mode is unsupported")
        elif rating_replay_mode is not None:
            raise LegacyImportError("applied legacy identity mapping non-replay mode is malformed")
        decision = LegacyIdentityMappingDecision(
            normalized_player_name=name,
            source_chain_key=_required_text(detail.get("source_chain_key"), field_name="source chain key"),
            source_record_keys=tuple(source_keys),
            game_account_id=detail.get("approved_game_account_id"),
            persona_id=detail.get("approved_persona_id"),
            disposition=disposition,
            rating_replay_mode=rating_replay_mode,
            relationship_snapshot=detail.get("relationship_snapshot"),
            operator_note=detail.get("operator_note"),
        )
        decisions[name] = decision
        submitted_by_name[name] = {
            "approved_game_account_id": decision.game_account_id,
            "approved_persona_id": decision.persona_id,
            "disposition": decision.disposition,
            "rating_replay_mode": decision.rating_replay_mode,
            "relationship_snapshot": decision.relationship_snapshot,
            "operator_note": decision.operator_note,
        }

    decision_manifest = build_legacy_identity_mapping_manifest(
        session,
        source_identifier=source,
        source_checksum=checksum,
    )
    decision_manifest["mapping_checksum"] = summary.get("mapping_checksum")
    for mapping in decision_manifest["mappings"]:
        name = mapping["normalized_player_name"]
        if name not in submitted_by_name:
            raise LegacyImportError("applied legacy identity mapping audit coverage is incomplete")
        mapping["decision"] = submitted_by_name[name]
    if set(submitted_by_name) != {mapping["normalized_player_name"] for mapping in decision_manifest["mappings"]}:
        raise LegacyImportError("applied legacy identity mapping audit coverage changed")
    decision_manifest["review"] = {
        "reviewed_by": summary.get("reviewed_by"),
        "reviewed_at": summary.get("reviewed_at"),
        "evidence_note": summary.get("evidence_note"),
    }
    plan = _build_decision_plan(
        session,
        decision_manifest=decision_manifest,
        allow_applied_candidate_changes=True,
    )
    _validate_current_source_owners(session, plan=plan, require_applied_snapshots=True)
    _validate_mapping_audit(session, run=run, plan=plan)
    if run.source_checksum != plan.decision_checksum or summary.get("decision_checksum") != plan.decision_checksum:
        raise LegacyImportError("applied legacy identity mapping decision checksum is invalid")
    if confirmation is not None and plan.decision_checksum != confirmation:
        raise LegacyImportError("confirmed mapping decision checksum changed")
    return AppliedLegacyIdentityMappingAudit(
        import_run_id=run.id,
        source_identifier=source,
        source_checksum=checksum,
        mapping_checksum=plan.mapping_checksum,
        decision_checksum=plan.decision_checksum,
        decision_manifest=decision_manifest,
        decisions=tuple(decisions[name] for name in sorted(decisions)),
    )


def _build_decision_plan(
    session: Session,
    *,
    decision_manifest: Mapping[str, object],
    allow_applied_candidate_changes: bool = False,
) -> _IdentityMappingDecisionPlan:
    if not isinstance(decision_manifest, Mapping):
        raise LegacyImportError("legacy identity mapping decision must be a JSON object")
    if decision_manifest.get("manifest_version") != MAPPING_MANIFEST_VERSION:
        raise LegacyImportError("legacy identity mapping manifest version is unsupported")
    source_identifier = normalize_import_source_identifier(
        _required_text(decision_manifest.get("source_identifier"), field_name="source identifier")
    )
    source_checksum = normalize_sha256_hex(
        _required_text(decision_manifest.get("source_checksum"), field_name="source checksum"),
        field_name="source checksum",
    )
    mapping_checksum = normalize_sha256_hex(
        _required_text(decision_manifest.get("mapping_checksum"), field_name="mapping checksum"),
        field_name="mapping checksum",
    )
    live_manifest = build_legacy_identity_mapping_manifest(
        session,
        source_identifier=source_identifier,
        source_checksum=source_checksum,
    )
    if not allow_applied_candidate_changes and live_manifest["mapping_checksum"] != mapping_checksum:
        raise LegacyImportError("legacy identity mapping source or candidate state changed after review")
    live_mappings = {
        mapping["normalized_player_name"]: mapping
        for mapping in live_manifest["mappings"]
        if isinstance(mapping, Mapping)
    }
    account_catalog = {
        account["game_account_id"]: account
        for account in live_manifest["game_accounts"]
        if isinstance(account, Mapping) and isinstance(account.get("game_account_id"), int)
    }

    submitted_mappings = decision_manifest.get("mappings")
    if not isinstance(submitted_mappings, (list, tuple)):
        raise LegacyImportError("legacy identity mapping decisions must be a list")
    approved: list[_ApprovedIdentityMapping] = []
    seen_names: set[str] = set()
    for submitted in submitted_mappings:
        if not isinstance(submitted, Mapping):
            raise LegacyImportError("legacy identity mapping decision row must be an object")
        normalized_name = _required_text(
            submitted.get("normalized_player_name"),
            field_name="normalized player name",
        )
        if normalized_name in seen_names or normalized_name not in live_mappings:
            raise LegacyImportError("legacy identity mapping decision names do not match the review template")
        seen_names.add(normalized_name)
        decision = submitted.get("decision")
        if not isinstance(decision, Mapping):
            raise LegacyImportError(f"legacy identity mapping decision is missing: {normalized_name}")
        disposition = _required_text(
            decision.get("disposition"),
            field_name=f"disposition for {normalized_name}",
        )
        if disposition not in LEGACY_IDENTITY_DISPOSITIONS:
            raise LegacyImportError(f"legacy identity mapping disposition is unsupported: {normalized_name}")
        game_account_id = decision.get("approved_game_account_id")
        if disposition == RATING_REPLAY:
            if not isinstance(game_account_id, int) or isinstance(game_account_id, bool) or game_account_id <= 0:
                raise LegacyImportError(f"legacy identity mapping replay target is missing: {normalized_name}")
            rating_replay_mode = _required_text(
                decision.get("rating_replay_mode", RATING_REPLAY_SOURCE_EXACT),
                field_name=f"Rating replay mode for {normalized_name}",
            )
            if rating_replay_mode not in RATING_REPLAY_MODES:
                raise LegacyImportError(f"legacy identity mapping replay mode is unsupported: {normalized_name}")
        elif game_account_id is not None:
            raise LegacyImportError(
                f"legacy identity mapping non-replay decision cannot target a GameAccount: {normalized_name}"
            )
        else:
            rating_replay_mode = decision.get("rating_replay_mode")
            if rating_replay_mode is not None:
                raise LegacyImportError(
                    f"legacy identity mapping non-replay decision cannot set a replay mode: {normalized_name}"
                )
        approved_persona_id = _optional_text(
            decision.get("approved_persona_id"),
            field_name=f"approved Persona ID for {normalized_name}",
            max_length=36,
        )
        if disposition != RATING_REPLAY and approved_persona_id is not None:
            raise LegacyImportError(
                f"legacy identity mapping non-replay decision cannot target a Persona: {normalized_name}"
            )
        operator_note = _optional_text(
            decision.get("operator_note"),
            field_name=f"operator note for {normalized_name}",
            max_length=1000,
        )
        if disposition != RATING_REPLAY and operator_note is None:
            raise LegacyImportError(
                f"legacy identity mapping non-replay decision requires an operator note: {normalized_name}"
            )
        live = live_mappings[normalized_name]
        if rating_replay_mode == RATING_REPLAY_RECALCULATE and int(live["rating_eligible_occurrence_count"]) <= 0:
            raise LegacyImportError(
                f"legacy identity mapping recalculation boundary has no Rating rows: {normalized_name}"
            )
        if rating_replay_mode == RATING_REPLAY_RECALCULATE and operator_note is None:
            raise LegacyImportError(
                f"legacy identity mapping reviewed recalculation requires an operator note: {normalized_name}"
            )
        account_detail = account_catalog.get(game_account_id) if game_account_id is not None else None
        if allow_applied_candidate_changes:
            relationship_snapshot = decision.get("relationship_snapshot")
            if relationship_snapshot is not None and not isinstance(relationship_snapshot, dict):
                raise LegacyImportError(
                    f"legacy identity mapping relationship snapshot is malformed: {normalized_name}"
                )
        else:
            relationship_snapshot = _relationship_snapshot(account_detail) if account_detail is not None else None
        approved.append(
            _ApprovedIdentityMapping(
                normalized_player_name=normalized_name,
                game_account_id=game_account_id,
                persona_id=approved_persona_id,
                disposition=disposition,
                rating_replay_mode=rating_replay_mode,
                operator_note=operator_note,
                relationship_snapshot=relationship_snapshot,
                source_player_names=tuple(live["source_player_names"]),
                source_record_keys=tuple(live["source_record_keys"]),
                occurrence_count=int(live["occurrence_count"]),
            )
        )
    if seen_names != set(live_mappings):
        raise LegacyImportError("legacy identity mapping decisions do not cover every source name")
    approved.sort(key=lambda mapping: mapping.normalized_player_name)
    approved_tuple = tuple(approved)
    _validate_approved_account_targets(session, mappings=approved_tuple)
    validate_legacy_rating_replay_chains(
        session,
        source_identifier=source_identifier,
        replay_targets_by_name={
            mapping.normalized_player_name: mapping.game_account_id
            for mapping in approved_tuple
            if mapping.disposition == RATING_REPLAY and mapping.game_account_id is not None
        },
        operator_notes_by_name={mapping.normalized_player_name: mapping.operator_note for mapping in approved_tuple},
        replay_modes_by_name={
            mapping.normalized_player_name: mapping.rating_replay_mode
            for mapping in approved_tuple
            if mapping.disposition == RATING_REPLAY and mapping.rating_replay_mode is not None
        },
    )

    review = decision_manifest.get("review")
    if not isinstance(review, Mapping):
        raise LegacyImportError("legacy identity mapping requires a two-reviewer record")
    reviewer_values = review.get("reviewed_by")
    if not isinstance(reviewer_values, (list, tuple)):
        raise LegacyImportError("legacy identity mapping requires two reviewers")
    reviewed_by = tuple(sorted({_required_text(value, field_name="reviewer") for value in reviewer_values}))
    if len(reviewed_by) < 2:
        raise LegacyImportError("legacy identity mapping requires two distinct reviewers")
    reviewed_at = _required_text(review.get("reviewed_at"), field_name="reviewed at")
    try:
        parsed_reviewed_at = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError:
        raise LegacyImportError("legacy identity mapping reviewed_at must be ISO-8601") from None
    if parsed_reviewed_at.tzinfo is None:
        raise LegacyImportError("legacy identity mapping reviewed_at must include a timezone")
    evidence_note = _optional_text(review.get("evidence_note"), field_name="evidence note", max_length=2000)

    decision_payload = {
        "manifest_version": MAPPING_MANIFEST_VERSION,
        "source_identifier": source_identifier,
        "source_checksum": source_checksum,
        "mapping_checksum": mapping_checksum,
        "reviewed_by": reviewed_by,
        "reviewed_at": reviewed_at,
        "evidence_note": evidence_note,
        "decisions": [
            {
                "normalized_player_name": mapping.normalized_player_name,
                "source_chain_key": _source_chain_key(
                    source_identifier=source_identifier,
                    normalized_player_name=mapping.normalized_player_name,
                ),
                "approved_game_account_id": mapping.game_account_id,
                "approved_persona_id": mapping.persona_id,
                "disposition": mapping.disposition,
                "rating_replay_mode": mapping.rating_replay_mode,
                "relationship_snapshot": mapping.relationship_snapshot,
                "operator_note": mapping.operator_note,
            }
            for mapping in approved
        ],
    }
    decision_checksum = sha256(
        json.dumps(decision_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return _IdentityMappingDecisionPlan(
        source_identifier=source_identifier,
        source_checksum=source_checksum,
        mapping_checksum=mapping_checksum,
        decision_checksum=decision_checksum,
        reviewed_by=reviewed_by,
        reviewed_at=reviewed_at,
        evidence_note=evidence_note,
        mappings=approved_tuple,
    )


def _validate_approved_account_targets(
    session: Session,
    *,
    mappings: tuple[_ApprovedIdentityMapping, ...],
) -> None:
    account_ids = {mapping.game_account_id for mapping in mappings if mapping.game_account_id is not None}
    accounts = {
        account.id: account for account in session.scalars(select(GameAccount).where(GameAccount.id.in_(account_ids)))
    }
    for mapping in mappings:
        if mapping.disposition != RATING_REPLAY:
            continue
        account = accounts.get(mapping.game_account_id)
        if account is None:
            raise LegacyImportError(f"legacy identity mapping target does not exist: {mapping.normalized_player_name}")
        if mapping.persona_id is not None and account.persona_id != mapping.persona_id:
            raise LegacyImportError(
                "legacy identity mapping Persona does not own the approved GameAccount: "
                f"{mapping.normalized_player_name}"
            )


def _validate_current_source_owners(
    session: Session,
    *,
    plan: _IdentityMappingDecisionPlan,
    require_applied_snapshots: bool,
) -> int:
    approved_by_name = {mapping.normalized_player_name: mapping for mapping in plan.mappings}
    source_rows = _source_identity_rows(session, source_identifier=plan.source_identifier)
    for entry, result, _grade in source_rows:
        normalized_name = normalize_player_name_strict(entry.player_name or "")
        approved = approved_by_name.get(normalized_name)
        if approved is None:
            raise LegacyImportError("legacy identity mapping source name changed after review")
        account_pair = (entry.game_account_id, result.game_account_id)
        snapshot_pair = (
            entry.owner_at_event_persona_id,
            result.owner_at_event_persona_id,
        )
        if approved.disposition == RATING_REPLAY:
            is_unlinked = (
                not require_applied_snapshots and account_pair == (None, None) and snapshot_pair == (None, None)
            )
            is_exact = account_pair == (
                approved.game_account_id,
                approved.game_account_id,
            ) and snapshot_pair == (approved.persona_id, approved.persona_id)
            is_linked_before_snapshot = (
                not require_applied_snapshots
                and account_pair == (approved.game_account_id, approved.game_account_id)
                and snapshot_pair == (None, None)
            )
            if not (is_unlinked or is_exact or is_linked_before_snapshot):
                raise LegacyImportError(
                    "legacy identity mapping source owner snapshot conflicts with the approved decision"
                )
        elif account_pair != (None, None) or snapshot_pair != (None, None):
            raise LegacyImportError("legacy identity mapping non-replay source must remain unlinked")
    return len(source_rows)


def _mapping_import_runs(
    session: Session,
    *,
    source_identifier: str,
    lock_rows: bool = False,
) -> tuple[SheetImportRun, ...]:
    statement = select(SheetImportRun).where(
        SheetImportRun.import_kind == LEGACY_IDENTITY_MAPPING_IMPORT_KIND,
        SheetImportRun.source_type == LEGACY_IDENTITY_MAPPING_SOURCE_TYPE,
        SheetImportRun.source_identifier == source_identifier,
        SheetImportRun.status == "completed",
        SheetImportRun.finished_at.is_not(None),
    )
    if lock_rows:
        statement = statement.with_for_update()
    return tuple(session.scalars(statement))


def _validate_mapping_audit(
    session: Session,
    *,
    run: SheetImportRun,
    plan: _IdentityMappingDecisionPlan,
) -> None:
    summary = run.summary_json if isinstance(run.summary_json, dict) else {}
    summary_reviewers = summary.get("reviewed_by")
    disposition_counts = plan.disposition_counts
    expected_replay_occurrences = _disposition_occurrence_count(plan, RATING_REPLAY)
    mapped_occurrences = summary.get("mapped_occurrence_count")
    skipped_occurrences = summary.get("skipped_occurrence_count")
    context_occurrences = summary.get("history_context_only_occurrence_count")
    unresolved_occurrences = summary.get("unresolved_history_occurrence_count")
    if (
        summary.get("mapping_manifest_version") != MAPPING_MANIFEST_VERSION
        or summary.get("source_checksum") != plan.source_checksum
        or summary.get("mapping_checksum") != plan.mapping_checksum
        or summary.get("decision_checksum") != plan.decision_checksum
        or summary.get("mapping_count") != len(plan.mappings)
        or summary.get("occurrence_count") != sum(mapping.occurrence_count for mapping in plan.mappings)
        or summary.get("rating_replay_count") != disposition_counts[RATING_REPLAY]
        or summary.get("history_context_only_count") != disposition_counts[HISTORY_CONTEXT_ONLY]
        or summary.get("unresolved_history_count") != disposition_counts[UNRESOLVED_HISTORY]
        or not isinstance(mapped_occurrences, int)
        or isinstance(mapped_occurrences, bool)
        or not isinstance(skipped_occurrences, int)
        or isinstance(skipped_occurrences, bool)
        or mapped_occurrences + skipped_occurrences != expected_replay_occurrences
        or context_occurrences != _disposition_occurrence_count(plan, HISTORY_CONTEXT_ONLY)
        or unresolved_occurrences != _disposition_occurrence_count(plan, UNRESOLVED_HISTORY)
        or not isinstance(summary_reviewers, (list, tuple))
        or tuple(sorted(summary_reviewers)) != plan.reviewed_by
        or summary.get("reviewed_at") != plan.reviewed_at
        or summary.get("evidence_note") != plan.evidence_note
    ):
        raise LegacyImportError("legacy identity mapping run summary no longer matches the approved decision")
    records = tuple(
        session.scalars(
            select(SheetImportRecord)
            .where(SheetImportRecord.import_run_id == run.id)
            .order_by(SheetImportRecord.source_row_number)
        )
    )
    if len(records) != len(plan.mappings):
        raise LegacyImportError("legacy identity mapping audit coverage changed")
    records_by_key = {record.source_key: record for record in records}
    for approved in plan.mappings:
        source_key = _mapping_record_source_key(
            source_identifier=plan.source_identifier,
            decision_checksum=plan.decision_checksum,
            normalized_player_name=approved.normalized_player_name,
        )
        record = records_by_key.get(source_key)
        detail = record.detail_json if record is not None and isinstance(record.detail_json, dict) else {}
        detail_source_names = detail.get("source_player_names")
        detail_source_keys = detail.get("source_record_keys")
        expected_target_type = "game_account" if approved.game_account_id is not None else None
        if (
            record is None
            or record.row_fingerprint != _mapping_record_fingerprint(approved)
            or record.record_type != LEGACY_IDENTITY_MAPPING_RECORD_TYPE
            or record.status != "applied"
            or record.target_entity_type != expected_target_type
            or record.target_entity_id != approved.game_account_id
            or detail.get("source_checksum") != plan.source_checksum
            or detail.get("mapping_checksum") != plan.mapping_checksum
            or detail.get("decision_checksum") != plan.decision_checksum
            or detail.get("normalized_player_name") != approved.normalized_player_name
            or detail.get("source_chain_key")
            != _source_chain_key(
                source_identifier=plan.source_identifier,
                normalized_player_name=approved.normalized_player_name,
            )
            or not isinstance(detail_source_names, (list, tuple))
            or tuple(detail_source_names) != approved.source_player_names
            or not isinstance(detail_source_keys, (list, tuple))
            or tuple(detail_source_keys) != approved.source_record_keys
            or detail.get("affected_occurrence_count") != approved.occurrence_count
            or detail.get("approved_game_account_id") != approved.game_account_id
            or detail.get("approved_persona_id") != approved.persona_id
            or detail.get("disposition") != approved.disposition
            or detail.get("rating_replay_mode") != approved.rating_replay_mode
            or detail.get("relationship_snapshot") != approved.relationship_snapshot
            or detail.get("operator_note") != approved.operator_note
        ):
            raise LegacyImportError("legacy identity mapping audit no longer matches the approved decision")


def _lock_identity_mapping_scope(
    session: Session,
    *,
    decision_manifest: Mapping[str, object],
) -> None:
    source_identifier = normalize_import_source_identifier(
        _required_text(decision_manifest.get("source_identifier"), field_name="source identifier")
    )
    tuple(
        session.scalars(
            select(SheetImportRun).where(SheetImportRun.source_identifier == source_identifier).with_for_update()
        )
    )
    tuple(
        session.scalars(
            select(RaceEntry)
            .join(Race, Race.id == RaceEntry.race_id)
            .where(Race.external_source == source_identifier, Race.race_kind == "room_match")
            .with_for_update()
        )
    )
    tuple(
        session.scalars(
            select(RaceResult)
            .join(Race, Race.id == RaceResult.race_id)
            .where(Race.external_source == source_identifier, Race.race_kind == "room_match")
            .with_for_update()
        )
    )
    tuple(session.scalars(select(GameAccount).with_for_update()))
    tuple(session.scalars(select(Persona).with_for_update()))


def _source_identity_rows(session: Session, *, source_identifier: str) -> tuple[object, ...]:
    return tuple(
        session.execute(
            select(RaceEntry, RaceResult, RaceCondition.grade)
            .join(Race, Race.id == RaceEntry.race_id)
            .join(
                RaceResult,
                and_(
                    RaceResult.race_id == RaceEntry.race_id,
                    RaceResult.entry_number == RaceEntry.entry_number,
                ),
            )
            .join(RaceCondition, RaceCondition.race_id == Race.id)
            .where(Race.external_source == source_identifier, Race.race_kind == "room_match")
            .order_by(Race.starts_at, Race.id, RaceEntry.entry_number)
        )
    )


def _mapping_record_source_key(
    *,
    source_identifier: str,
    decision_checksum: str,
    normalized_player_name: str,
) -> str:
    payload = (
        f"{LEGACY_IDENTITY_MAPPING_IMPORT_KIND}\0{source_identifier}\0{decision_checksum}\0{normalized_player_name}"
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _mapping_record_fingerprint(mapping: _ApprovedIdentityMapping) -> str:
    payload = {
        "normalized_player_name": mapping.normalized_player_name,
        "approved_game_account_id": mapping.game_account_id,
        "approved_persona_id": mapping.persona_id,
        "disposition": mapping.disposition,
        "rating_replay_mode": mapping.rating_replay_mode,
        "relationship_snapshot": mapping.relationship_snapshot,
        "operator_note": mapping.operator_note,
        "source_player_names": mapping.source_player_names,
        "source_record_keys": mapping.source_record_keys,
        "occurrence_count": mapping.occurrence_count,
    }
    return sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _source_chain_key(*, source_identifier: str, normalized_player_name: str) -> str:
    payload = f"{LEGACY_IDENTITY_MAPPING_IMPORT_KIND}\0{source_identifier}\0{normalized_player_name}"
    return sha256(payload.encode("utf-8")).hexdigest()


def _relationship_snapshot(account_detail: Mapping[str, object]) -> dict[str, object]:
    peer_ids = account_detail.get("persona_peer_game_account_ids")
    return {
        "game_account_id": account_detail.get("game_account_id"),
        "persona_id": account_detail.get("persona_id"),
        "persona_peer_game_account_ids": list(peer_ids) if isinstance(peer_ids, (list, tuple)) else [],
        "identity_status": account_detail.get("identity_status"),
        "is_source_only": account_detail.get("is_source_only"),
    }


def _disposition_occurrence_count(plan: _IdentityMappingDecisionPlan, disposition: str) -> int:
    return sum(mapping.occurrence_count for mapping in plan.mappings if mapping.disposition == disposition)


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LegacyImportError(f"legacy identity mapping {field_name} is required")
    return value.strip()


def _optional_text(value: object, *, field_name: str, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LegacyImportError(f"legacy identity mapping {field_name} must be text")
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise LegacyImportError(f"legacy identity mapping {field_name} is too long")
    return normalized


def _require_clean_session(session: Session) -> None:
    if session.new or session.dirty or session.deleted:
        raise LegacyImportError("legacy identity mapping requires a clean session")


def _validate_result_provenance(
    *,
    source_rows: tuple[object, ...],
    result_run_id: int,
    session: Session,
) -> dict[int, SheetImportRecord]:
    record_ids = {
        entry.source_import_record_id
        for entry, _result, _grade in source_rows
        if entry.source_import_record_id is not None
    }
    records = {
        record.id: record
        for record in session.scalars(select(SheetImportRecord).where(SheetImportRecord.id.in_(record_ids)))
    }
    for entry, result, _grade in source_rows:
        record_id = entry.source_import_record_id
        record = records.get(record_id) if record_id is not None else None
        if (
            record is None
            or result.source_import_record_id != record_id
            or record.import_run_id != result_run_id
            or record.record_type != LEGACY_RESULT_RECORD_TYPE
            or record.status != "applied"
            or record.target_entity_type != "race_result"
            or record.target_entity_id != result.id
        ):
            raise LegacyImportError("legacy identity mapping source provenance is incomplete")
    return records


def _identity_source_keys(
    session: Session,
    *,
    source_identifier: str,
    source_checksum: str,
) -> dict[int, list[str]]:
    rows = tuple(
        session.execute(
            select(SheetImportRecord.target_entity_id, SheetImportRecord.source_key)
            .join(SheetImportRun, SheetImportRun.id == SheetImportRecord.import_run_id)
            .where(
                SheetImportRun.source_type == "xlsx",
                SheetImportRun.source_identifier == source_identifier,
                SheetImportRun.source_checksum == source_checksum,
                SheetImportRun.status == "completed",
                SheetImportRecord.record_type == LEGACY_IDENTITY_POINT_RECORD_TYPE,
                SheetImportRecord.status == "applied",
                SheetImportRecord.target_entity_type == "game_account",
                SheetImportRecord.target_entity_id.is_not(None),
            )
            .order_by(SheetImportRecord.target_entity_id, SheetImportRecord.source_key)
        )
    )
    source_keys: dict[int, list[str]] = defaultdict(list)
    for target_entity_id, source_key in rows:
        source_keys[int(target_entity_id)].append(source_key)
    return source_keys


def _mapping_status(
    *,
    current_account_ids: tuple[int, ...],
    candidate_account_ids: tuple[int, ...],
    unmapped_occurrence_count: int,
    inconsistent_occurrence_count: int,
) -> tuple[str, int | None, int | None]:
    if inconsistent_occurrence_count or len(current_account_ids) > 1:
        return "conflict", None, None
    if current_account_ids:
        account_id = current_account_ids[0]
        if unmapped_occurrence_count:
            return "partially_mapped", None, None
        return "already_mapped", None, account_id
    if not candidate_account_ids:
        return "unresolved", None, None
    if len(candidate_account_ids) > 1:
        return "ambiguous_exact", None, None
    account_id = candidate_account_ids[0]
    return "exact_candidate", account_id, None
