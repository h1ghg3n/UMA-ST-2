from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import SheetImportRecord, SheetImportRun
from umacircle_bot.domain.errors import LegacyImportError
from umacircle_bot.domain.imports import normalize_import_source_identifier, normalize_sha256_hex
from umacircle_bot.services.legacy_identity_mapping import (
    HISTORY_CONTEXT_ONLY,
    RATING_REPLAY,
    UNRESOLVED_HISTORY,
    LegacyIdentityMappingDecision,
    load_applied_legacy_identity_mapping_audit,
)
from umacircle_bot.services.legacy_rating_chain import (
    RATING_REPLAY_RECALCULATE,
    LegacyRatingChainRow,
    legacy_rating_chain_issues,
    legacy_rating_chain_row_sort_key,
    load_legacy_rating_chain_rows_by_name,
)

LEGACY_RATING_CHAIN_DISPOSITION_IMPORT_KIND = "legacy_room_rating_chain_disposition"
LEGACY_RATING_CHAIN_DISPOSITION_SOURCE_TYPE = "operator_manifest"
LEGACY_RATING_CHAIN_DISPOSITION_MANIFEST_VERSION = 2
LEGACY_RATING_CHAIN_DISPOSITION_RECORD_TYPE = "legacy_rating_chain_disposition"
LEGACY_RATING_CHAIN_DISPOSITION_SHEET_NAME = "rating_chain_disposition"


@dataclass(frozen=True, slots=True)
class LegacyRatingChainDispositionPreview:
    source_identifier: str
    source_checksum: str
    mapping_checksum: str
    mapping_decision_checksum: str
    disposition_checksum: str
    decision_checksum: str
    chain_count: int
    result_count: int
    rating_replay_count: int
    history_context_only_count: int
    unresolved_history_count: int
    already_applied: bool


@dataclass(frozen=True, slots=True)
class LegacyRatingChainDispositionApplyResult:
    import_run_id: int
    source_identifier: str
    source_checksum: str
    mapping_checksum: str
    mapping_decision_checksum: str
    disposition_checksum: str
    decision_checksum: str
    chain_count: int
    result_count: int


@dataclass(frozen=True, slots=True)
class LegacyRatingChainDispositionAudit:
    import_run_id: int
    source_identifier: str
    source_checksum: str
    mapping_checksum: str
    mapping_decision_checksum: str
    disposition_checksum: str
    decision_checksum: str
    replay_result_ids: frozenset[int]
    recalculation_trigger_result_ids: frozenset[int]
    history_context_only_result_ids: frozenset[int]
    unresolved_history_result_ids: frozenset[int]

    @property
    def context_result_ids(self) -> frozenset[int]:
        return self.history_context_only_result_ids


AppliedLegacyRatingChainDispositions = LegacyRatingChainDispositionAudit


@dataclass(frozen=True, slots=True)
class _ApprovedChain:
    source_chain_key: str
    source_mapping_chain_keys: tuple[str, ...]
    normalized_player_names: tuple[str, ...]
    source_record_keys: tuple[str, ...]
    ordered_snapshot_evidence: tuple[dict[str, object], ...]
    game_account_id: int | None
    persona_id: str | None
    disposition: str
    rating_replay_modes: tuple[str | None, ...]
    relationship_snapshot: dict[str, object] | None
    operator_note: str | None


@dataclass(frozen=True, slots=True)
class _DecisionPlan:
    source_identifier: str
    source_checksum: str
    mapping_checksum: str
    mapping_decision_checksum: str
    disposition_checksum: str
    decision_checksum: str
    reviewed_by: tuple[str, ...]
    reviewed_at: str
    evidence_note: str | None
    chains: tuple[_ApprovedChain, ...]


def build_legacy_rating_chain_disposition_manifest(
    session: Session,
    source_identifier: str,
    source_checksum: str,
) -> dict[str, object]:
    """Build a deterministic S3 review template from the live, applied S2 audit."""

    _require_clean_session(session)
    source = normalize_import_source_identifier(source_identifier)
    checksum = normalize_sha256_hex(source_checksum, field_name="source checksum")
    mapping_manifest, mapping_decision_checksum, s2_decisions = _load_valid_s2(
        session,
        source_identifier=source,
        source_checksum=checksum,
    )
    rows_by_name = load_legacy_rating_chain_rows_by_name(session, source_identifier=source)
    if not rows_by_name:
        raise LegacyImportError("legacy rating chain disposition requires Rating-eligible results")
    if set(rows_by_name) - set(s2_decisions):
        raise LegacyImportError("legacy rating chain disposition S2 coverage is incomplete")

    grouped: dict[tuple[object, ...], list[LegacyIdentityMappingDecision]] = defaultdict(list)
    for name in sorted(rows_by_name):
        decision = s2_decisions[name]
        if decision.disposition == RATING_REPLAY:
            group_key = (RATING_REPLAY, decision.game_account_id)
        else:
            group_key = (decision.disposition, decision.source_chain_key)
        grouped[group_key].append(decision)

    chains: list[dict[str, object]] = []
    for decisions in grouped.values():
        decisions.sort(key=lambda item: item.normalized_player_name)
        first = decisions[0]
        if any(
            item.disposition != first.disposition
            or item.game_account_id != first.game_account_id
            or item.persona_id != first.persona_id
            or item.relationship_snapshot != first.relationship_snapshot
            for item in decisions
        ):
            raise LegacyImportError("legacy rating chain disposition S2 alias target is inconsistent")
        rows = tuple(
            sorted(
                (row for item in decisions for row in rows_by_name.get(item.normalized_player_name, ())),
                key=legacy_rating_chain_row_sort_key,
            )
        )
        row_issues = tuple(f"{row.source_record_key}:{issue}" for row in rows for issue in row.issues)
        if row_issues:
            raise LegacyImportError(f"legacy rating chain disposition source row is incomplete: {row_issues[0]}")
        if first.disposition == RATING_REPLAY:
            recalculation_boundary_source_keys = {
                rows_by_name[item.normalized_player_name][0].source_record_key
                for item in decisions
                if item.rating_replay_mode == RATING_REPLAY_RECALCULATE
                and rows_by_name.get(item.normalized_player_name)
            }
            issues = legacy_rating_chain_issues(
                rows,
                recalculation_boundary_source_keys=recalculation_boundary_source_keys,
            )
            if issues:
                raise LegacyImportError(f"legacy rating chain disposition replay chain is incomplete: {issues[0]}")
        source_mapping_chain_keys = tuple(item.source_chain_key for item in decisions)
        chain_key = _aggregate_chain_key(
            source_identifier=source,
            disposition=first.disposition,
            source_mapping_chain_keys=source_mapping_chain_keys,
        )
        evidence = tuple(_row_evidence(row) for row in rows)
        source_record_keys = tuple(item["source_record_key"] for item in evidence)
        chains.append(
            {
                "source_chain_key": chain_key,
                "source_mapping_chain_keys": source_mapping_chain_keys,
                "normalized_player_names": tuple(item.normalized_player_name for item in decisions),
                "source_record_keys": source_record_keys,
                "ordered_snapshot_evidence": evidence,
                "approved_game_account_id": first.game_account_id,
                "approved_persona_id": first.persona_id,
                "relationship_snapshot": first.relationship_snapshot,
                "disposition": first.disposition,
                "rating_replay_modes": tuple(item.rating_replay_mode for item in decisions),
                "s2_operator_notes": tuple(item.operator_note for item in decisions),
                "decision": {
                    "approved_game_account_id": first.game_account_id,
                    "approved_persona_id": first.persona_id,
                    "disposition": first.disposition,
                    "operator_note": first.operator_note,
                },
            }
        )
    chains.sort(key=lambda item: item["source_chain_key"])
    basis = {
        "manifest_version": LEGACY_RATING_CHAIN_DISPOSITION_MANIFEST_VERSION,
        "source_identifier": source,
        "source_checksum": checksum,
        "mapping_checksum": mapping_manifest["mapping_checksum"],
        "mapping_decision_checksum": mapping_decision_checksum,
        "chains": [_chain_basis(chain) for chain in chains],
    }
    disposition_checksum = _json_checksum(basis)
    counts = Counter(chain["disposition"] for chain in chains)
    return {
        "manifest_version": LEGACY_RATING_CHAIN_DISPOSITION_MANIFEST_VERSION,
        "source_identifier": source,
        "source_checksum": checksum,
        "mapping_checksum": mapping_manifest["mapping_checksum"],
        "mapping_decision_checksum": mapping_decision_checksum,
        "chains": chains,
        "disposition_checksum": disposition_checksum,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "review_template",
        "writes_database": False,
        "review": {"reviewed_by": (), "reviewed_at": None, "evidence_note": None},
        "summary": {
            "chain_count": len(chains),
            "result_count": sum(len(chain["source_record_keys"]) for chain in chains),
            "rating_replay_count": counts[RATING_REPLAY],
            "history_context_only_count": counts[HISTORY_CONTEXT_ONLY],
            "unresolved_history_count": counts[UNRESOLVED_HISTORY],
        },
    }


def preview_legacy_rating_chain_disposition(
    session: Session,
    *,
    decision_manifest: Mapping[str, object],
) -> LegacyRatingChainDispositionPreview:
    _require_clean_session(session)
    plan = _build_decision_plan(session, decision_manifest=decision_manifest)
    runs = _disposition_runs(session, source_identifier=plan.source_identifier)
    if len(runs) > 1:
        raise LegacyImportError("legacy rating chain disposition has multiple completed apply runs")
    if runs:
        if runs[0].source_checksum != plan.decision_checksum:
            raise LegacyImportError("legacy rating chain disposition was already applied with a different decision")
        _validate_audit(session, run=runs[0], plan=plan)
    counts = Counter(chain.disposition for chain in plan.chains)
    return LegacyRatingChainDispositionPreview(
        source_identifier=plan.source_identifier,
        source_checksum=plan.source_checksum,
        mapping_checksum=plan.mapping_checksum,
        mapping_decision_checksum=plan.mapping_decision_checksum,
        disposition_checksum=plan.disposition_checksum,
        decision_checksum=plan.decision_checksum,
        chain_count=len(plan.chains),
        result_count=sum(len(chain.source_record_keys) for chain in plan.chains),
        rating_replay_count=counts[RATING_REPLAY],
        history_context_only_count=counts[HISTORY_CONTEXT_ONLY],
        unresolved_history_count=counts[UNRESOLVED_HISTORY],
        already_applied=bool(runs),
    )


def apply_legacy_rating_chain_disposition(
    session: Session,
    *,
    decision_manifest: Mapping[str, object],
    confirmed_source_checksum: str,
    confirmed_decision_checksum: str,
) -> LegacyRatingChainDispositionApplyResult:
    """Persist only S3 approval records; imported and economic rows are untouched."""

    _require_clean_session(session)
    source_confirmation = normalize_sha256_hex(confirmed_source_checksum, field_name="confirmed source checksum")
    decision_confirmation = normalize_sha256_hex(confirmed_decision_checksum, field_name="confirmed decision checksum")
    with session.begin_nested():
        source = normalize_import_source_identifier(
            _required_text(decision_manifest.get("source_identifier"), field_name="source identifier")
        )
        tuple(
            session.scalars(select(SheetImportRun).where(SheetImportRun.source_identifier == source).with_for_update())
        )
        plan = _build_decision_plan(session, decision_manifest=decision_manifest)
        if plan.source_checksum != source_confirmation:
            raise LegacyImportError("confirmed source checksum does not match the rating chain disposition decision")
        if plan.decision_checksum != decision_confirmation:
            raise LegacyImportError("confirmed decision checksum does not match the rating chain disposition decision")
        runs = _disposition_runs(session, source_identifier=plan.source_identifier, lock_rows=True)
        if len(runs) > 1:
            raise LegacyImportError("legacy rating chain disposition has multiple completed apply runs")
        if runs:
            run = runs[0]
            if run.source_checksum != plan.decision_checksum:
                raise LegacyImportError("legacy rating chain disposition was already applied with a different decision")
            _validate_audit(session, run=run, plan=plan, lock_rows=True)
            return _apply_result(run.id, plan)

        run = SheetImportRun(
            import_kind=LEGACY_RATING_CHAIN_DISPOSITION_IMPORT_KIND,
            source_type=LEGACY_RATING_CHAIN_DISPOSITION_SOURCE_TYPE,
            source_identifier=plan.source_identifier,
            source_checksum=plan.decision_checksum,
            status="running",
        )
        session.add(run)
        session.flush()
        for row_number, chain in enumerate(plan.chains, start=1):
            target_type = "game_account" if chain.game_account_id is not None else None
            session.add(
                SheetImportRecord(
                    import_run_id=run.id,
                    source_key=_audit_source_key(plan, chain),
                    row_fingerprint=_chain_fingerprint(chain),
                    source_sheet_name=LEGACY_RATING_CHAIN_DISPOSITION_SHEET_NAME,
                    source_row_number=row_number,
                    record_type=LEGACY_RATING_CHAIN_DISPOSITION_RECORD_TYPE,
                    status="applied",
                    target_entity_type=target_type,
                    target_entity_id=chain.game_account_id,
                    detail_json=_chain_detail(plan, chain),
                )
            )
        now = datetime.now(UTC)
        counts = Counter(chain.disposition for chain in plan.chains)
        run.status = "completed"
        run.finished_at = now
        run.summary_json = {
            "manifest_version": LEGACY_RATING_CHAIN_DISPOSITION_MANIFEST_VERSION,
            "source_checksum": plan.source_checksum,
            "mapping_checksum": plan.mapping_checksum,
            "mapping_decision_checksum": plan.mapping_decision_checksum,
            "disposition_checksum": plan.disposition_checksum,
            "decision_checksum": plan.decision_checksum,
            "chain_count": len(plan.chains),
            "result_count": sum(len(chain.source_record_keys) for chain in plan.chains),
            "rating_replay_count": counts[RATING_REPLAY],
            "history_context_only_count": counts[HISTORY_CONTEXT_ONLY],
            "unresolved_history_count": counts[UNRESOLVED_HISTORY],
            "reviewed_by": list(plan.reviewed_by),
            "reviewed_at": plan.reviewed_at,
            "evidence_note": plan.evidence_note,
            "writes_domain_rows": False,
        }
        session.flush()
        _validate_audit(session, run=run, plan=plan)
        return _apply_result(run.id, plan)


def load_applied_legacy_rating_chain_dispositions(
    session: Session,
    source_identifier: str,
    source_checksum: str,
    confirmed_decision_checksum: str | None = None,
    lock_rows: bool = False,
) -> LegacyRatingChainDispositionAudit:
    """Load and fully revalidate the live S3 audit for backfill/preflight use."""

    _require_clean_session(session)
    source = normalize_import_source_identifier(source_identifier)
    checksum = normalize_sha256_hex(source_checksum, field_name="source checksum")
    runs = _disposition_runs(session, source_identifier=source, lock_rows=lock_rows)
    if len(runs) != 1:
        raise LegacyImportError("legacy rating chain disposition requires one completed apply run")
    run = runs[0]
    summary = run.summary_json if isinstance(run.summary_json, dict) else {}
    decision_checksum = normalize_sha256_hex(
        _required_text(summary.get("decision_checksum"), field_name="audit decision checksum"),
        field_name="audit decision checksum",
    )
    if confirmed_decision_checksum is not None and decision_checksum != normalize_sha256_hex(
        confirmed_decision_checksum, field_name="confirmed decision checksum"
    ):
        raise LegacyImportError("confirmed decision checksum does not match the applied disposition")
    manifest = _manifest_from_audit(
        session,
        run=run,
        source_checksum=checksum,
        lock_rows=lock_rows,
    )
    plan = _build_decision_plan(session, decision_manifest=manifest)
    if run.source_checksum != plan.decision_checksum:
        raise LegacyImportError("legacy rating chain disposition audit decision checksum changed")
    _validate_audit(session, run=run, plan=plan, lock_rows=lock_rows)

    _mapping_manifest, mapping_decision_checksum, _s2_decisions = _load_valid_s2(
        session,
        source_identifier=source,
        source_checksum=checksum,
    )
    if mapping_decision_checksum != plan.mapping_decision_checksum:
        raise LegacyImportError("legacy rating chain disposition S2 decision changed")
    rows_by_name = load_legacy_rating_chain_rows_by_name(session, source_identifier=source)
    result_ids_by_key: dict[str, int] = {}
    for rows in rows_by_name.values():
        for row in rows:
            if row.source_record_key in result_ids_by_key:
                raise LegacyImportError("legacy rating chain disposition source key is duplicated")
            result_ids_by_key[row.source_record_key] = row.result_id
    covered = [key for chain in plan.chains for key in chain.source_record_keys]
    if len(covered) != len(set(covered)) or set(covered) != set(result_ids_by_key):
        raise LegacyImportError("legacy rating chain disposition result coverage changed")
    result_sets: dict[str, set[int]] = defaultdict(set)
    recalculation_trigger_result_ids: set[int] = set()
    for chain in plan.chains:
        result_sets[chain.disposition].update(result_ids_by_key[key] for key in chain.source_record_keys)
        if len(chain.normalized_player_names) != len(chain.rating_replay_modes):
            raise LegacyImportError("legacy rating chain disposition replay mode coverage changed")
        for normalized_name, replay_mode in zip(
            chain.normalized_player_names,
            chain.rating_replay_modes,
            strict=True,
        ):
            if replay_mode != RATING_REPLAY_RECALCULATE:
                continue
            rows = rows_by_name.get(normalized_name, ())
            if not rows:
                raise LegacyImportError("legacy rating chain disposition recalculation boundary changed")
            recalculation_trigger_result_ids.add(rows[0].result_id)
    return LegacyRatingChainDispositionAudit(
        import_run_id=run.id,
        source_identifier=source,
        source_checksum=checksum,
        mapping_checksum=plan.mapping_checksum,
        mapping_decision_checksum=plan.mapping_decision_checksum,
        disposition_checksum=plan.disposition_checksum,
        decision_checksum=plan.decision_checksum,
        replay_result_ids=frozenset(result_sets[RATING_REPLAY]),
        recalculation_trigger_result_ids=frozenset(recalculation_trigger_result_ids),
        history_context_only_result_ids=frozenset(result_sets[HISTORY_CONTEXT_ONLY]),
        unresolved_history_result_ids=frozenset(result_sets[UNRESOLVED_HISTORY]),
    )


def _build_decision_plan(session: Session, *, decision_manifest: Mapping[str, object]) -> _DecisionPlan:
    if not isinstance(decision_manifest, Mapping):
        raise LegacyImportError("legacy rating chain disposition decision must be a JSON object")
    if decision_manifest.get("manifest_version") != LEGACY_RATING_CHAIN_DISPOSITION_MANIFEST_VERSION:
        raise LegacyImportError("legacy rating chain disposition manifest version is unsupported")
    source = normalize_import_source_identifier(
        _required_text(decision_manifest.get("source_identifier"), field_name="source identifier")
    )
    checksum = normalize_sha256_hex(
        _required_text(decision_manifest.get("source_checksum"), field_name="source checksum"),
        field_name="source checksum",
    )
    live = build_legacy_rating_chain_disposition_manifest(session, source, checksum)
    mapping_checksum = normalize_sha256_hex(
        _required_text(decision_manifest.get("mapping_checksum"), field_name="mapping checksum"),
        field_name="mapping checksum",
    )
    mapping_decision_checksum = normalize_sha256_hex(
        _required_text(
            decision_manifest.get("mapping_decision_checksum"),
            field_name="mapping decision checksum",
        ),
        field_name="mapping decision checksum",
    )
    disposition_checksum = normalize_sha256_hex(
        _required_text(decision_manifest.get("disposition_checksum"), field_name="disposition checksum"),
        field_name="disposition checksum",
    )
    if (
        live["mapping_checksum"] != mapping_checksum
        or live["mapping_decision_checksum"] != mapping_decision_checksum
        or live["disposition_checksum"] != disposition_checksum
    ):
        raise LegacyImportError("legacy rating chain disposition source or mapping basis changed after review")
    live_chains = {chain["source_chain_key"]: chain for chain in live["chains"]}
    submitted = decision_manifest.get("chains")
    if not isinstance(submitted, (list, tuple)):
        raise LegacyImportError("legacy rating chain disposition decisions must be a list")
    approved: list[_ApprovedChain] = []
    seen: set[str] = set()
    for item in submitted:
        if not isinstance(item, Mapping):
            raise LegacyImportError("legacy rating chain disposition decision row must be an object")
        key = _required_text(item.get("source_chain_key"), field_name="source chain key")
        current = live_chains.get(key)
        if (
            current is None
            or key in seen
            or _json_checksum(_chain_basis(item)) != _json_checksum(_chain_basis(current))
        ):
            raise LegacyImportError("legacy rating chain disposition chains do not match the review template")
        seen.add(key)
        decision = item.get("decision")
        if not isinstance(decision, Mapping):
            raise LegacyImportError("legacy rating chain disposition decision is missing")
        disposition = _required_text(decision.get("disposition"), field_name="disposition")
        game_account_id = decision.get("approved_game_account_id")
        persona_id = decision.get("approved_persona_id")
        if (
            disposition != current["disposition"]
            or game_account_id != current["approved_game_account_id"]
            or persona_id != current["approved_persona_id"]
        ):
            raise LegacyImportError("legacy rating chain disposition must exactly reapprove S2")
        operator_note = _optional_text(decision.get("operator_note"), field_name="operator note", max_length=1000)
        if disposition != RATING_REPLAY and operator_note is None:
            raise LegacyImportError("legacy rating chain disposition non-replay decision requires an operator note")
        approved.append(
            _ApprovedChain(
                source_chain_key=key,
                source_mapping_chain_keys=tuple(current["source_mapping_chain_keys"]),
                normalized_player_names=tuple(current["normalized_player_names"]),
                source_record_keys=tuple(current["source_record_keys"]),
                ordered_snapshot_evidence=tuple(current["ordered_snapshot_evidence"]),
                game_account_id=game_account_id,
                persona_id=persona_id,
                disposition=disposition,
                rating_replay_modes=tuple(current["rating_replay_modes"]),
                relationship_snapshot=current["relationship_snapshot"],
                operator_note=operator_note,
            )
        )
    if seen != set(live_chains):
        raise LegacyImportError("legacy rating chain disposition must cover every chain")
    approved.sort(key=lambda chain: chain.source_chain_key)

    review = decision_manifest.get("review")
    if not isinstance(review, Mapping):
        raise LegacyImportError("legacy rating chain disposition requires a two-reviewer record")
    values = review.get("reviewed_by")
    if not isinstance(values, (list, tuple)):
        raise LegacyImportError("legacy rating chain disposition requires two reviewers")
    reviewed_by = tuple(sorted({_required_text(value, field_name="reviewer") for value in values}))
    if len(reviewed_by) < 2:
        raise LegacyImportError("legacy rating chain disposition requires two distinct reviewers")
    reviewed_at = _required_text(review.get("reviewed_at"), field_name="reviewed at")
    try:
        parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError:
        raise LegacyImportError("legacy rating chain disposition reviewed_at must be ISO-8601") from None
    if parsed.tzinfo is None:
        raise LegacyImportError("legacy rating chain disposition reviewed_at must include a timezone")
    evidence_note = _optional_text(review.get("evidence_note"), field_name="evidence note", max_length=2000)
    decision_payload = {
        "manifest_version": LEGACY_RATING_CHAIN_DISPOSITION_MANIFEST_VERSION,
        "source_identifier": source,
        "source_checksum": checksum,
        "mapping_checksum": mapping_checksum,
        "mapping_decision_checksum": mapping_decision_checksum,
        "disposition_checksum": disposition_checksum,
        "reviewed_by": reviewed_by,
        "reviewed_at": reviewed_at,
        "evidence_note": evidence_note,
        "decisions": [
            {
                "source_chain_key": chain.source_chain_key,
                "source_mapping_chain_keys": chain.source_mapping_chain_keys,
                "approved_game_account_id": chain.game_account_id,
                "approved_persona_id": chain.persona_id,
                "disposition": chain.disposition,
                "rating_replay_modes": chain.rating_replay_modes,
                "relationship_snapshot": chain.relationship_snapshot,
                "operator_note": chain.operator_note,
            }
            for chain in approved
        ],
    }
    return _DecisionPlan(
        source_identifier=source,
        source_checksum=checksum,
        mapping_checksum=mapping_checksum,
        mapping_decision_checksum=mapping_decision_checksum,
        disposition_checksum=disposition_checksum,
        decision_checksum=_json_checksum(decision_payload),
        reviewed_by=reviewed_by,
        reviewed_at=reviewed_at,
        evidence_note=evidence_note,
        chains=tuple(approved),
    )


def _load_valid_s2(
    session: Session, *, source_identifier: str, source_checksum: str
) -> tuple[dict[str, object], str, dict[str, LegacyIdentityMappingDecision]]:
    audit = load_applied_legacy_identity_mapping_audit(
        session,
        source_identifier=source_identifier,
        source_checksum=source_checksum,
    )
    return (
        audit.decision_manifest,
        audit.decision_checksum,
        {decision.normalized_player_name: decision for decision in audit.decisions},
    )


def _validate_audit(
    session: Session,
    *,
    run: SheetImportRun,
    plan: _DecisionPlan,
    lock_rows: bool = False,
) -> None:
    summary = run.summary_json if isinstance(run.summary_json, dict) else {}
    counts = Counter(chain.disposition for chain in plan.chains)
    if summary != {
        "manifest_version": LEGACY_RATING_CHAIN_DISPOSITION_MANIFEST_VERSION,
        "source_checksum": plan.source_checksum,
        "mapping_checksum": plan.mapping_checksum,
        "mapping_decision_checksum": plan.mapping_decision_checksum,
        "disposition_checksum": plan.disposition_checksum,
        "decision_checksum": plan.decision_checksum,
        "chain_count": len(plan.chains),
        "result_count": sum(len(chain.source_record_keys) for chain in plan.chains),
        "rating_replay_count": counts[RATING_REPLAY],
        "history_context_only_count": counts[HISTORY_CONTEXT_ONLY],
        "unresolved_history_count": counts[UNRESOLVED_HISTORY],
        "reviewed_by": list(plan.reviewed_by),
        "reviewed_at": plan.reviewed_at,
        "evidence_note": plan.evidence_note,
        "writes_domain_rows": False,
    }:
        raise LegacyImportError("legacy rating chain disposition audit summary was tampered")
    statement = (
        select(SheetImportRecord)
        .where(SheetImportRecord.import_run_id == run.id)
        .order_by(SheetImportRecord.source_row_number)
    )
    if lock_rows:
        statement = statement.with_for_update()
    records = tuple(session.scalars(statement))
    if len(records) != len(plan.chains):
        raise LegacyImportError("legacy rating chain disposition audit coverage changed")
    by_key = {record.source_key: record for record in records}
    for chain in plan.chains:
        record = by_key.get(_audit_source_key(plan, chain))
        expected_type = "game_account" if chain.game_account_id is not None else None
        if (
            record is None
            or record.row_fingerprint != _chain_fingerprint(chain)
            or record.record_type != LEGACY_RATING_CHAIN_DISPOSITION_RECORD_TYPE
            or record.status != "applied"
            or record.target_entity_type != expected_type
            or record.target_entity_id != chain.game_account_id
            or _json_checksum(record.detail_json) != _json_checksum(_chain_detail(plan, chain))
        ):
            raise LegacyImportError("legacy rating chain disposition audit record was tampered")


def _manifest_from_audit(
    session: Session,
    *,
    run: SheetImportRun,
    source_checksum: str,
    lock_rows: bool,
) -> dict[str, object]:
    live = build_legacy_rating_chain_disposition_manifest(session, run.source_identifier, source_checksum)
    summary = run.summary_json if isinstance(run.summary_json, dict) else {}
    statement = select(SheetImportRecord).where(SheetImportRecord.import_run_id == run.id)
    if lock_rows:
        statement = statement.with_for_update()
    records = tuple(session.scalars(statement))
    decisions: dict[str, Mapping[str, object]] = {}
    for record in records:
        detail = record.detail_json if isinstance(record.detail_json, Mapping) else {}
        key = detail.get("source_chain_key")
        if isinstance(key, str):
            decisions[key] = {
                "approved_game_account_id": detail.get("approved_game_account_id"),
                "approved_persona_id": detail.get("approved_persona_id"),
                "disposition": detail.get("disposition"),
                "operator_note": detail.get("operator_note"),
            }
    for chain in live["chains"]:
        chain["decision"] = decisions.get(chain["source_chain_key"])
    live["review"] = {
        "reviewed_by": summary.get("reviewed_by"),
        "reviewed_at": summary.get("reviewed_at"),
        "evidence_note": summary.get("evidence_note"),
    }
    return live


def _chain_basis(chain: Mapping[str, object]) -> dict[str, object]:
    return {
        "source_chain_key": chain.get("source_chain_key"),
        "source_mapping_chain_keys": chain.get("source_mapping_chain_keys"),
        "normalized_player_names": chain.get("normalized_player_names"),
        "source_record_keys": chain.get("source_record_keys"),
        "ordered_snapshot_evidence": chain.get("ordered_snapshot_evidence"),
        "approved_game_account_id": chain.get("approved_game_account_id"),
        "approved_persona_id": chain.get("approved_persona_id"),
        "relationship_snapshot": chain.get("relationship_snapshot"),
        "disposition": chain.get("disposition"),
        "rating_replay_modes": chain.get("rating_replay_modes"),
        "s2_operator_notes": chain.get("s2_operator_notes"),
    }


def _row_evidence(row: LegacyRatingChainRow) -> dict[str, object]:
    return {
        "source_record_key": row.source_record_key,
        "starts_at": row.starts_at.isoformat() if row.starts_at is not None else None,
        "snapshot": {key: format(value, "f") for key, value in sorted(row.snapshot.items())},
        "issues": tuple(row.issues),
    }


def _aggregate_chain_key(
    *, source_identifier: str, disposition: str, source_mapping_chain_keys: tuple[str, ...]
) -> str:
    return _json_checksum(
        {
            "kind": LEGACY_RATING_CHAIN_DISPOSITION_IMPORT_KIND,
            "source_identifier": source_identifier,
            "disposition": disposition,
            "source_mapping_chain_keys": source_mapping_chain_keys,
        }
    )


def _audit_source_key(plan: _DecisionPlan, chain: _ApprovedChain) -> str:
    return _json_checksum(
        {
            "kind": LEGACY_RATING_CHAIN_DISPOSITION_IMPORT_KIND,
            "source_identifier": plan.source_identifier,
            "decision_checksum": plan.decision_checksum,
            "source_chain_key": chain.source_chain_key,
        }
    )


def _chain_fingerprint(chain: _ApprovedChain) -> str:
    return _json_checksum(
        {
            "source_chain_key": chain.source_chain_key,
            "source_mapping_chain_keys": chain.source_mapping_chain_keys,
            "normalized_player_names": chain.normalized_player_names,
            "source_record_keys": chain.source_record_keys,
            "ordered_snapshot_evidence": chain.ordered_snapshot_evidence,
            "approved_game_account_id": chain.game_account_id,
            "approved_persona_id": chain.persona_id,
            "relationship_snapshot": chain.relationship_snapshot,
            "disposition": chain.disposition,
            "rating_replay_modes": chain.rating_replay_modes,
            "operator_note": chain.operator_note,
        }
    )


def _chain_detail(plan: _DecisionPlan, chain: _ApprovedChain) -> dict[str, object]:
    return {
        "manifest_version": LEGACY_RATING_CHAIN_DISPOSITION_MANIFEST_VERSION,
        "source_checksum": plan.source_checksum,
        "mapping_checksum": plan.mapping_checksum,
        "mapping_decision_checksum": plan.mapping_decision_checksum,
        "disposition_checksum": plan.disposition_checksum,
        "decision_checksum": plan.decision_checksum,
        "source_chain_key": chain.source_chain_key,
        "source_mapping_chain_keys": list(chain.source_mapping_chain_keys),
        "normalized_player_names": list(chain.normalized_player_names),
        "source_record_keys": list(chain.source_record_keys),
        "ordered_snapshot_evidence": list(chain.ordered_snapshot_evidence),
        "approved_game_account_id": chain.game_account_id,
        "approved_persona_id": chain.persona_id,
        "relationship_snapshot": chain.relationship_snapshot,
        "disposition": chain.disposition,
        "rating_replay_modes": list(chain.rating_replay_modes),
        "operator_note": chain.operator_note,
    }


def _disposition_runs(
    session: Session, *, source_identifier: str, lock_rows: bool = False
) -> tuple[SheetImportRun, ...]:
    statement = select(SheetImportRun).where(
        SheetImportRun.import_kind == LEGACY_RATING_CHAIN_DISPOSITION_IMPORT_KIND,
        SheetImportRun.source_type == LEGACY_RATING_CHAIN_DISPOSITION_SOURCE_TYPE,
        SheetImportRun.source_identifier == source_identifier,
        SheetImportRun.status == "completed",
        SheetImportRun.finished_at.is_not(None),
    )
    if lock_rows:
        statement = statement.with_for_update()
    return tuple(session.scalars(statement))


def _apply_result(run_id: int, plan: _DecisionPlan) -> LegacyRatingChainDispositionApplyResult:
    return LegacyRatingChainDispositionApplyResult(
        import_run_id=run_id,
        source_identifier=plan.source_identifier,
        source_checksum=plan.source_checksum,
        mapping_checksum=plan.mapping_checksum,
        mapping_decision_checksum=plan.mapping_decision_checksum,
        disposition_checksum=plan.disposition_checksum,
        decision_checksum=plan.decision_checksum,
        chain_count=len(plan.chains),
        result_count=sum(len(chain.source_record_keys) for chain in plan.chains),
    )


def _json_checksum(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LegacyImportError(f"legacy rating chain disposition {field_name} is required")
    return value.strip()


def _optional_text(value: object, *, field_name: str, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LegacyImportError(f"legacy rating chain disposition {field_name} must be text")
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise LegacyImportError(f"legacy rating chain disposition {field_name} is too long")
    return normalized


def _require_clean_session(session: Session) -> None:
    if session.new or session.dirty or session.deleted:
        raise LegacyImportError("legacy rating chain disposition requires a clean session")
