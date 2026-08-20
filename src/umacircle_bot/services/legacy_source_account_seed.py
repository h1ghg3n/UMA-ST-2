from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    CirclePointAccount,
    CirclePointTransaction,
    GameAccount,
    Persona,
    RaceRatingContext,
    RatingEvent,
    SheetImportRecord,
    SheetImportRun,
)
from umacircle_bot.domain.errors import LegacyImportError
from umacircle_bot.domain.imports import normalize_import_source_identifier, normalize_sha256_hex
from umacircle_bot.services._legacy_source_account_seed_contract import (
    CREATE_SOURCE_ONLY,
    LEGACY_SOURCE_ACCOUNT_SEED_IMPORT_KIND,
    LEGACY_SOURCE_ACCOUNT_SEED_RECORD_TYPE,
    LEGACY_SOURCE_ACCOUNT_SEED_SHEET_NAME,
    LEGACY_SOURCE_ACCOUNT_SEED_SOURCE_TYPE,
    SOURCE_ACCOUNT_SEED_MANIFEST_VERSION,
    AppliedLegacySourceAccountSeed,
    LegacySourceAccountSeedApplyResult,
    LegacySourceAccountSeedPreview,
    _candidate_basis,
    _checksum,
    _decision_basis,
    _decision_checksum,
    _manifest_identity,
    _optional_text,
    _parse_decision,
    _record_fingerprint,
    _record_source_key,
    _required_text,
    _SeedDecision,
    _SeedPlan,
)
from umacircle_bot.services._legacy_source_account_seed_contract import (
    _build_submitted_plan as _contract_build_submitted_plan,
)
from umacircle_bot.services._legacy_source_account_seed_contract import (
    _record_detail as _contract_record_detail,
)
from umacircle_bot.services._legacy_source_account_seed_contract import (
    _run_summary as _contract_run_summary,
)
from umacircle_bot.services.legacy_identity_mapping import (
    LEGACY_IDENTITY_MAPPING_IMPORT_KIND,
    build_legacy_identity_mapping_manifest,
)
from umacircle_bot.services.legacy_result_import import LEGACY_RESULT_IMPORT_KIND

SOURCE_ACCOUNT_SEED_AUDIT_CONTRACT_VERSION = 2


def _build_submitted_plan(
    decision_manifest: Mapping[str, object],
    *,
    live_template: Mapping[str, object] | None = None,
) -> _SeedPlan:
    if decision_manifest.get("audit_contract_version") != SOURCE_ACCOUNT_SEED_AUDIT_CONTRACT_VERSION:
        raise LegacyImportError("legacy source-account seed audit contract version is unsupported")
    return _contract_build_submitted_plan(decision_manifest, live_template=live_template)


def _run_summary(plan: _SeedPlan, *, created_count: int) -> dict[str, object]:
    return {
        **_contract_run_summary(plan, created_count=created_count),
        "audit_contract_version": SOURCE_ACCOUNT_SEED_AUDIT_CONTRACT_VERSION,
    }


def _record_detail(
    decision: _SeedDecision,
    *,
    plan: _SeedPlan,
    persona_id: str | None,
    circle_point_account_id: int | None,
) -> dict[str, object]:
    return {
        **_contract_record_detail(decision, plan=plan),
        "audit_contract_version": SOURCE_ACCOUNT_SEED_AUDIT_CONTRACT_VERSION,
        "persona_id": persona_id,
        "circle_point_account_id": circle_point_account_id,
    }


def build_legacy_source_account_seed_manifest(
    session: Session,
    *,
    source_identifier: str,
    source_checksum: str,
) -> dict[str, object]:
    """Build a read-only review template for pending source-only GameAccounts."""

    _require_clean_session(session)
    normalized_source = normalize_import_source_identifier(source_identifier)
    normalized_checksum = normalize_sha256_hex(source_checksum, field_name="source checksum")
    _require_result_import(
        session,
        source_identifier=normalized_source,
        source_checksum=normalized_checksum,
        lock=False,
    )
    if _seed_runs(session, source_identifier=normalized_source):
        raise LegacyImportError("legacy source-account seed was already applied; retain the approved artifact")
    _require_before_mapping_and_rating(session, source_identifier=normalized_source)
    return _build_live_template(
        session,
        source_identifier=normalized_source,
        source_checksum=normalized_checksum,
    )


def preview_legacy_source_account_seed(
    session: Session,
    *,
    decision_manifest: Mapping[str, object],
) -> LegacySourceAccountSeedPreview:
    _require_clean_session(session)
    source_identifier, source_checksum = _manifest_identity(decision_manifest)
    _require_result_import(
        session,
        source_identifier=source_identifier,
        source_checksum=source_checksum,
        lock=False,
    )
    runs = _seed_runs(session, source_identifier=source_identifier)
    if len(runs) > 1:
        raise LegacyImportError("legacy source-account seed has multiple completed apply runs")
    if runs:
        plan = _build_submitted_plan(decision_manifest)
        _validate_live_source_rows(session, plan=plan)
        _validate_seed_provenance_audit(session, run=runs[0], plan=plan)
    else:
        _require_before_mapping_and_rating(session, source_identifier=source_identifier)
        live_template = _build_live_template(
            session,
            source_identifier=source_identifier,
            source_checksum=source_checksum,
        )
        plan = _build_submitted_plan(decision_manifest, live_template=live_template)
    return LegacySourceAccountSeedPreview(
        source_identifier=plan.source_identifier,
        source_checksum=plan.source_checksum,
        seed_basis_checksum=plan.seed_basis_checksum,
        decision_checksum=plan.decision_checksum,
        decision_count=len(plan.decisions),
        create_count=plan.create_count,
        no_seed_count=plan.no_seed_count,
        already_applied=bool(runs),
    )


def apply_legacy_source_account_seed(
    session: Session,
    *,
    decision_manifest: Mapping[str, object],
    confirmed_source_checksum: str,
    confirmed_decision_checksum: str,
) -> LegacySourceAccountSeedApplyResult:
    """Create reviewed source Personas, pending GameAccounts, and empty wallets."""

    _require_clean_session(session)
    source_identifier, source_checksum = _manifest_identity(decision_manifest)
    normalized_source_confirmation = normalize_sha256_hex(
        confirmed_source_checksum,
        field_name="confirmed source checksum",
    )
    normalized_decision_confirmation = normalize_sha256_hex(
        confirmed_decision_checksum,
        field_name="confirmed decision checksum",
    )
    with session.begin_nested():
        _require_result_import(
            session,
            source_identifier=source_identifier,
            source_checksum=source_checksum,
            lock=True,
        )
        runs = _seed_runs(session, source_identifier=source_identifier, lock=True)
        if len(runs) > 1:
            raise LegacyImportError("legacy source-account seed has multiple completed apply runs")
        if runs:
            plan = _build_submitted_plan(decision_manifest)
            _validate_live_source_rows(session, plan=plan)
            _validate_seed_provenance_audit(session, run=runs[0], plan=plan)
            _validate_confirmations(
                plan,
                source_checksum=normalized_source_confirmation,
                decision_checksum=normalized_decision_confirmation,
            )
            return LegacySourceAccountSeedApplyResult(
                import_run_id=runs[0].id,
                source_identifier=plan.source_identifier,
                source_checksum=plan.source_checksum,
                seed_basis_checksum=plan.seed_basis_checksum,
                decision_checksum=plan.decision_checksum,
                decision_count=len(plan.decisions),
                created_count=0,
                no_seed_count=plan.no_seed_count,
                exact_retry=True,
            )

        _require_before_mapping_and_rating(session, source_identifier=source_identifier)
        live_template = _build_live_template(
            session,
            source_identifier=source_identifier,
            source_checksum=source_checksum,
        )
        plan = _build_submitted_plan(decision_manifest, live_template=live_template)
        _validate_confirmations(
            plan,
            source_checksum=normalized_source_confirmation,
            decision_checksum=normalized_decision_confirmation,
        )
        run = SheetImportRun(
            import_kind=LEGACY_SOURCE_ACCOUNT_SEED_IMPORT_KIND,
            source_type=LEGACY_SOURCE_ACCOUNT_SEED_SOURCE_TYPE,
            source_identifier=plan.source_identifier,
            source_checksum=plan.decision_checksum,
            status="running",
        )
        session.add(run)
        session.flush()
        created_count = 0
        for row_number, decision in enumerate(plan.decisions, start=1):
            account = None
            persona = None
            circle_point_account = None
            if decision.action == CREATE_SOURCE_ONLY:
                persona = Persona(
                    display_name=decision.game_account_display_name,
                    display_name_source="manual",
                    status="inactive",
                )
                account = GameAccount(
                    nickname=decision.game_account_display_name,
                    ingame_name=decision.game_account_display_name,
                    identity_status="pending_identity",
                    persona=persona,
                )
                circle_point_account = CirclePointAccount(persona=persona, balance=0)
                session.add_all((persona, account, circle_point_account))
                session.flush()
                created_count += 1
            session.add(
                SheetImportRecord(
                    import_run_id=run.id,
                    source_key=_record_source_key(plan=plan, decision=decision),
                    row_fingerprint=_record_fingerprint(decision),
                    source_sheet_name=LEGACY_SOURCE_ACCOUNT_SEED_SHEET_NAME,
                    source_row_number=row_number,
                    record_type=LEGACY_SOURCE_ACCOUNT_SEED_RECORD_TYPE,
                    status="applied",
                    target_entity_type="game_account" if account is not None else None,
                    target_entity_id=account.id if account is not None else None,
                    detail_json=_record_detail(
                        decision,
                        plan=plan,
                        persona_id=persona.id if persona is not None else None,
                        circle_point_account_id=(circle_point_account.id if circle_point_account is not None else None),
                    ),
                )
            )
        run.status = "completed"
        run.finished_at = datetime.now(UTC)
        run.summary_json = _run_summary(plan, created_count=created_count)
        session.flush()
        _validate_seed_creation_audit(session, run=run, plan=plan)

    return LegacySourceAccountSeedApplyResult(
        import_run_id=run.id,
        source_identifier=plan.source_identifier,
        source_checksum=plan.source_checksum,
        seed_basis_checksum=plan.seed_basis_checksum,
        decision_checksum=plan.decision_checksum,
        decision_count=len(plan.decisions),
        created_count=created_count,
        no_seed_count=plan.no_seed_count,
        exact_retry=False,
    )


def load_applied_legacy_source_account_seed(
    session: Session,
    *,
    source_identifier: str,
    source_checksum: str,
) -> AppliedLegacySourceAccountSeed | None:
    """Load and revalidate the self-contained source-account seed audit."""

    _require_clean_session(session)
    normalized_source = normalize_import_source_identifier(source_identifier)
    normalized_checksum = normalize_sha256_hex(source_checksum, field_name="source checksum")
    runs = _seed_runs(session, source_identifier=normalized_source)
    if not runs:
        return None
    if len(runs) != 1:
        raise LegacyImportError("legacy source-account seed has multiple completed apply runs")
    run = runs[0]
    summary = run.summary_json
    if not isinstance(summary, Mapping):
        raise LegacyImportError("legacy source-account seed audit summary changed")
    seed_basis_checksum = normalize_sha256_hex(
        _required_text(summary.get("seed_basis_checksum"), field="seed basis checksum"),
        field_name="seed basis checksum",
    )
    if summary.get("source_checksum") != normalized_checksum:
        raise LegacyImportError("legacy source-account seed source checksum changed")
    reviewer_values = summary.get("reviewed_by")
    if not isinstance(reviewer_values, (list, tuple)):
        raise LegacyImportError("legacy source-account seed audit reviewers changed")
    reviewed_by = tuple(sorted({_required_text(value, field="reviewer") for value in reviewer_values}))
    if len(reviewed_by) < 2:
        raise LegacyImportError("legacy source-account seed audit reviewers changed")
    reviewed_at = _required_text(summary.get("reviewed_at"), field="reviewed at")
    try:
        parsed_reviewed_at = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError:
        raise LegacyImportError("legacy source-account seed audit reviewed_at changed") from None
    if parsed_reviewed_at.tzinfo is None:
        raise LegacyImportError("legacy source-account seed audit reviewed_at changed")
    evidence_note = _optional_text(summary.get("evidence_note"), field="evidence note", maximum=2000)
    records = tuple(
        session.scalars(
            select(SheetImportRecord)
            .where(SheetImportRecord.import_run_id == run.id)
            .order_by(SheetImportRecord.source_row_number)
        )
    )
    if not records:
        raise LegacyImportError("legacy source-account seed audit coverage changed")
    decisions = tuple(_decision_from_record(record, source_checksum=normalized_checksum) for record in records)
    if len({decision.normalized_player_name for decision in decisions}) != len(decisions):
        raise LegacyImportError("legacy source-account seed audit contains duplicate source names")
    plan = _SeedPlan(
        source_identifier=normalized_source,
        source_checksum=normalized_checksum,
        seed_basis_checksum=seed_basis_checksum,
        decision_checksum=_decision_checksum(
            source_identifier=normalized_source,
            source_checksum=normalized_checksum,
            seed_basis_checksum=seed_basis_checksum,
            reviewed_by=reviewed_by,
            reviewed_at=reviewed_at,
            evidence_note=evidence_note,
            decisions=decisions,
        ),
        reviewed_by=reviewed_by,
        reviewed_at=reviewed_at,
        evidence_note=evidence_note,
        decisions=decisions,
    )
    _validate_seed_provenance_audit(session, run=run, plan=plan)
    created_ids = tuple(
        int(record.target_entity_id)
        for record in records
        if record.target_entity_type == "game_account" and record.target_entity_id is not None
    )
    return AppliedLegacySourceAccountSeed(
        import_run_id=run.id,
        source_identifier=normalized_source,
        source_checksum=normalized_checksum,
        seed_basis_checksum=seed_basis_checksum,
        decision_checksum=plan.decision_checksum,
        decision_count=len(decisions),
        created_game_account_ids=created_ids,
        no_seed_count=plan.no_seed_count,
    )


def _build_live_template(
    session: Session,
    *,
    source_identifier: str,
    source_checksum: str,
) -> dict[str, object]:
    mapping = build_legacy_identity_mapping_manifest(
        session,
        source_identifier=source_identifier,
        source_checksum=source_checksum,
    )
    candidates = [
        {
            "normalized_player_name": row["normalized_player_name"],
            "source_chain_key": row["source_chain_key"],
            "source_player_names": list(row["source_player_names"]),
            "source_record_keys": list(row["source_record_keys"]),
            "occurrence_count": row["occurrence_count"],
            "rating_eligible_occurrence_count": row["rating_eligible_occurrence_count"],
            "exact_candidate_game_account_ids": list(row["exact_candidate_game_account_ids"]),
            "status": row["status"],
        }
        for row in mapping["mappings"]
    ]
    basis = {
        "manifest_version": SOURCE_ACCOUNT_SEED_MANIFEST_VERSION,
        "audit_contract_version": SOURCE_ACCOUNT_SEED_AUDIT_CONTRACT_VERSION,
        "source_identifier": source_identifier,
        "source_checksum": source_checksum,
        "mapping_checksum": mapping["mapping_checksum"],
        "candidates": candidates,
    }
    seed_basis_checksum = _checksum(basis)
    return {
        **basis,
        "seed_basis_checksum": seed_basis_checksum,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "review_template",
        "writes_database": False,
        "review": {
            "reviewed_by": [],
            "reviewed_at": None,
            "evidence_note": None,
        },
        "candidates": [
            {
                **candidate,
                "decision": {
                    "action": None,
                    "game_account_display_name": candidate["source_player_names"][0],
                    "operator_note": None,
                },
            }
            for candidate in candidates
        ],
        "summary": {
            "candidate_count": len(candidates),
            "unresolved_count": sum(candidate["status"] == "unresolved" for candidate in candidates),
            "exact_candidate_count": sum(candidate["status"] == "exact_candidate" for candidate in candidates),
            "ambiguous_count": sum(candidate["status"] == "ambiguous_exact" for candidate in candidates),
        },
    }


def _decision_from_record(
    record: SheetImportRecord,
    *,
    source_checksum: str,
) -> _SeedDecision:
    detail = record.detail_json
    if (
        not isinstance(detail, Mapping)
        or detail.get("manifest_version") != SOURCE_ACCOUNT_SEED_MANIFEST_VERSION
        or detail.get("audit_contract_version") != SOURCE_ACCOUNT_SEED_AUDIT_CONTRACT_VERSION
        or detail.get("source_checksum") != source_checksum
    ):
        raise LegacyImportError("legacy source-account seed audit record changed")
    return _parse_decision(
        {
            "normalized_player_name": detail.get("normalized_player_name"),
            "source_chain_key": detail.get("source_chain_key"),
            "source_player_names": detail.get("source_player_names"),
            "source_record_keys": detail.get("source_record_keys"),
            "occurrence_count": detail.get("occurrence_count"),
            "rating_eligible_occurrence_count": detail.get("rating_eligible_occurrence_count"),
            "exact_candidate_game_account_ids": detail.get("exact_candidate_game_account_ids"),
            "status": detail.get("status"),
            "decision": {
                "action": detail.get("action"),
                "game_account_display_name": detail.get("game_account_display_name"),
                "operator_note": detail.get("operator_note"),
            },
        }
    )


def _validate_live_source_rows(session: Session, *, plan: _SeedPlan) -> None:
    live = _build_live_template(
        session,
        source_identifier=plan.source_identifier,
        source_checksum=plan.source_checksum,
    )
    source_fields = (
        "normalized_player_name",
        "source_chain_key",
        "source_player_names",
        "source_record_keys",
        "occurrence_count",
        "rating_eligible_occurrence_count",
    )
    live_rows = {
        str(candidate["normalized_player_name"]): {key: _candidate_basis(candidate)[key] for key in source_fields}
        for candidate in live["candidates"]
        if isinstance(candidate, Mapping)
    }
    submitted_rows = {
        decision.normalized_player_name: {key: _decision_basis(decision)[key] for key in source_fields}
        for decision in plan.decisions
    }
    if submitted_rows != live_rows:
        raise LegacyImportError("legacy source-account seed source rows changed after apply")


def _validate_seed_creation_audit(session: Session, *, run: SheetImportRun, plan: _SeedPlan) -> None:
    _validate_seed_audit(session, run=run, plan=plan, require_creation_state=True)


def _validate_seed_provenance_audit(session: Session, *, run: SheetImportRun, plan: _SeedPlan) -> None:
    _validate_seed_audit(session, run=run, plan=plan, require_creation_state=False)


def _validate_seed_audit(
    session: Session,
    *,
    run: SheetImportRun,
    plan: _SeedPlan,
    require_creation_state: bool,
) -> None:
    if (
        run.source_type != LEGACY_SOURCE_ACCOUNT_SEED_SOURCE_TYPE
        or run.source_checksum != plan.decision_checksum
        or run.status != "completed"
        or run.finished_at is None
        or run.summary_json != _run_summary(plan, created_count=plan.create_count)
    ):
        raise LegacyImportError("legacy source-account seed audit summary changed")
    records = tuple(
        session.scalars(
            select(SheetImportRecord)
            .where(SheetImportRecord.import_run_id == run.id)
            .order_by(SheetImportRecord.source_row_number)
        )
    )
    if len(records) != len(plan.decisions):
        raise LegacyImportError("legacy source-account seed audit coverage changed")
    created_account_ids: set[int] = set()
    for row_number, (record, decision) in enumerate(zip(records, plan.decisions, strict=True), start=1):
        detail = record.detail_json
        if not isinstance(detail, Mapping):
            raise LegacyImportError("legacy source-account seed audit record changed")
        persona_id = detail.get("persona_id")
        circle_point_account_id = detail.get("circle_point_account_id")
        if (
            record.source_key != _record_source_key(plan=plan, decision=decision)
            or record.row_fingerprint != _record_fingerprint(decision)
            or record.source_sheet_name != LEGACY_SOURCE_ACCOUNT_SEED_SHEET_NAME
            or record.source_row_number != row_number
            or record.record_type != LEGACY_SOURCE_ACCOUNT_SEED_RECORD_TYPE
            or record.status != "applied"
            or record.detail_json
            != _record_detail(
                decision,
                plan=plan,
                persona_id=persona_id if isinstance(persona_id, str) else None,
                circle_point_account_id=(circle_point_account_id if isinstance(circle_point_account_id, int) else None),
            )
        ):
            raise LegacyImportError("legacy source-account seed audit record changed")
        if decision.action == CREATE_SOURCE_ONLY:
            if require_creation_state:
                _validate_created_seed_creation_state(
                    session,
                    record=record,
                    decision=decision,
                    persona_id=persona_id,
                    circle_point_account_id=circle_point_account_id,
                )
            else:
                _validate_created_seed_provenance(
                    session,
                    record=record,
                    persona_id=persona_id,
                    circle_point_account_id=circle_point_account_id,
                )
            created_account_ids.add(record.target_entity_id)
        elif (
            record.target_entity_type is not None
            or record.target_entity_id is not None
            or persona_id is not None
            or circle_point_account_id is not None
        ):
            raise LegacyImportError("legacy source-account no-seed audit unexpectedly has a target")
    if len(created_account_ids) != plan.create_count:
        raise LegacyImportError("legacy source-account seed target GameAccounts are not unique")


def _validate_created_seed_creation_state(
    session: Session,
    *,
    record: SheetImportRecord,
    decision: _SeedDecision,
    persona_id: object,
    circle_point_account_id: object,
) -> None:
    account, persona, circle_point_account = _load_created_seed_targets(
        session,
        record=record,
        persona_id=persona_id,
        circle_point_account_id=circle_point_account_id,
    )
    if (
        account.discord_account_id is not None
        or account.uma_pid is not None
        or account.nickname != decision.game_account_display_name
        or account.ingame_name != decision.game_account_display_name
        or account.identity_status != "pending_identity"
        or persona.display_name != decision.game_account_display_name
        or persona.display_name_source != "manual"
        or persona.status != "inactive"
        or circle_point_account.balance != 0
    ):
        raise LegacyImportError("legacy source-account seed target state changed during creation")
    if (
        session.scalar(
            select(GameAccount.id).where(GameAccount.persona_id == persona.id, GameAccount.id != account.id).limit(1)
        )
        is not None
    ):
        raise LegacyImportError("legacy source-account seed Persona has unexpected GameAccounts during creation")
    if (
        session.scalar(
            select(CirclePointTransaction.id)
            .where(
                (CirclePointTransaction.persona_id == persona.id)
                | (CirclePointTransaction.game_account_id == account.id)
            )
            .limit(1)
        )
        is not None
    ):
        raise LegacyImportError("legacy source-account seed wallet unexpectedly has ledger history during creation")


def _validate_created_seed_provenance(
    session: Session,
    *,
    record: SheetImportRecord,
    persona_id: object,
    circle_point_account_id: object,
) -> None:
    _load_created_seed_targets(
        session,
        record=record,
        persona_id=persona_id,
        circle_point_account_id=circle_point_account_id,
    )


def _load_created_seed_targets(
    session: Session,
    *,
    record: SheetImportRecord,
    persona_id: object,
    circle_point_account_id: object,
) -> tuple[GameAccount, Persona, CirclePointAccount]:
    if (
        record.target_entity_type != "game_account"
        or not isinstance(record.target_entity_id, int)
        or not isinstance(persona_id, str)
        or not persona_id
        or not isinstance(circle_point_account_id, int)
        or isinstance(circle_point_account_id, bool)
    ):
        raise LegacyImportError("legacy source-account seed target references changed")
    account = session.get(GameAccount, record.target_entity_id)
    persona = session.get(Persona, persona_id)
    circle_point_account = session.get(CirclePointAccount, circle_point_account_id)
    if account is None or persona is None or circle_point_account is None:
        raise LegacyImportError("legacy source-account seed target is missing")
    if account.persona_id != persona.id or circle_point_account.persona_id != persona.id:
        raise LegacyImportError("legacy source-account seed target provenance changed")
    return account, persona, circle_point_account


def _validate_confirmations(plan: _SeedPlan, *, source_checksum: str, decision_checksum: str) -> None:
    if plan.source_checksum != source_checksum:
        raise LegacyImportError("confirmed source checksum does not match the source-account decision")
    if plan.decision_checksum != decision_checksum:
        raise LegacyImportError("confirmed decision checksum does not match the source-account decision")


def _require_result_import(
    session: Session,
    *,
    source_identifier: str,
    source_checksum: str,
    lock: bool,
) -> SheetImportRun:
    statement = select(SheetImportRun).where(
        SheetImportRun.import_kind == LEGACY_RESULT_IMPORT_KIND,
        SheetImportRun.source_type == "xlsx",
        SheetImportRun.source_identifier == source_identifier,
        SheetImportRun.source_checksum == source_checksum,
        SheetImportRun.status == "completed",
        SheetImportRun.finished_at.is_not(None),
    )
    if lock:
        statement = statement.order_by(SheetImportRun.id).with_for_update()
    runs = tuple(session.scalars(statement))
    if len(runs) != 1:
        raise LegacyImportError("legacy source-account seed requires one confirmed result import run")
    return runs[0]


def _require_before_mapping_and_rating(session: Session, *, source_identifier: str) -> None:
    if (
        session.scalar(
            select(SheetImportRun.id)
            .where(
                SheetImportRun.import_kind == LEGACY_IDENTITY_MAPPING_IMPORT_KIND,
                SheetImportRun.source_identifier == source_identifier,
            )
            .limit(1)
        )
        is not None
    ):
        raise LegacyImportError("legacy source-account seed must run before identity mapping")
    if session.scalar(select(RatingEvent.id).limit(1)) is not None:
        raise LegacyImportError("legacy source-account seed must run before Rating events exist")
    if session.scalar(select(RaceRatingContext.id).limit(1)) is not None:
        raise LegacyImportError("legacy source-account seed must run before Rating contexts exist")


def _seed_runs(session: Session, *, source_identifier: str, lock: bool = False) -> tuple[SheetImportRun, ...]:
    statement = select(SheetImportRun).where(
        SheetImportRun.import_kind == LEGACY_SOURCE_ACCOUNT_SEED_IMPORT_KIND,
        SheetImportRun.source_identifier == source_identifier,
    )
    if lock:
        statement = statement.order_by(SheetImportRun.id).with_for_update()
    runs = tuple(session.scalars(statement))
    if any(run.status != "completed" or run.finished_at is None for run in runs):
        raise LegacyImportError("legacy source-account seed has an incomplete apply run")
    return runs


def _require_clean_session(session: Session) -> None:
    if session.new or session.dirty or session.deleted:
        raise LegacyImportError("legacy source-account seed requires a clean session")
