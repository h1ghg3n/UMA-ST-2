from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    CirclePointAccount,
    GameAccount,
    Race,
    RaceCondition,
    RaceEntry,
    RaceResult,
    SheetImportRecord,
)
from umacircle_bot.domain.errors import (
    LegacyImportError,
    MatchPlacementRewardError,
)
from umacircle_bot.domain.imports import normalize_import_source_identifier, normalize_sha256_hex
from umacircle_bot.domain.match_placement_rewards import (
    MatchPlacementCandidate,
    allocate_match_placement_rewards,
    placement_reward_policy_basis,
)
from umacircle_bot.services.legacy_identity_mapping import (
    HISTORY_CONTEXT_ONLY,
    RATING_REPLAY,
    UNRESOLVED_HISTORY,
    AppliedLegacyIdentityMappingAudit,
    LegacyIdentityMappingDecision,
)

HISTORICAL_PLACEMENT_CORRECTION_VERSION = 1
HISTORICAL_PLACEMENT_CORRECTION_IMPORT_KIND = "historical_match_placement_correction"
HISTORICAL_PLACEMENT_CORRECTION_SOURCE_TYPE = "derived_history"
HISTORICAL_PLACEMENT_CORRECTION_RECORD_TYPE = "historical_match_placement_correction"
HISTORICAL_PLACEMENT_CORRECTION_SHEET_NAME = "historical_match_placement_correction"
HISTORICAL_PLACEMENT_TRANSACTION_TYPE = "match_placement_reward"
HISTORICAL_PLACEMENT_TRANSACTION_SOURCE = "historical_match_placement_correction"
HISTORICAL_PLACEMENT_TARGET_EXTERNAL_IDS = ("52", "53", "54", "55", "56")


@dataclass(frozen=True, slots=True)
class HistoricalPlacementConflict:
    code: str
    race_business_key: str | None = None
    result_business_key: str | None = None


@dataclass(frozen=True, slots=True)
class HistoricalPlacementResultManifestRow:
    result_business_key: str
    entry_number: int
    rank: int
    attribution_status: str
    is_rating_excluded: bool
    is_result_void: bool
    game_account_business_key: str | None
    owner_business_key: str | None
    outcome: str
    amount: int
    selected_result_business_key: str | None = None


@dataclass(frozen=True, slots=True)
class HistoricalPlacementRaceManifestRow:
    race_business_key: str
    external_race_id: str
    grade: str
    participant_count: int
    result_rows: tuple[HistoricalPlacementResultManifestRow, ...]
    delta: int


@dataclass(frozen=True, slots=True)
class HistoricalPlacementCorrectionManifest:
    manifest_version: int
    source_identifier: str
    source_checksum: str
    e2_business_key_prefix_checksum: str
    mapping_checksum: str
    mapping_decision_checksum: str
    target_attribution_checksum: str
    reward_policy_checksum: str
    target_external_race_ids: tuple[str, ...]
    races: tuple[HistoricalPlacementRaceManifestRow, ...]
    per_owner_deltas: tuple[dict[str, object], ...]
    expected_total: int
    conflicts: tuple[HistoricalPlacementConflict, ...]
    manifest_checksum: str

    @property
    def ready(self) -> bool:
        return not self.conflicts

    def as_dict(self) -> dict[str, object]:
        return _json_value({**asdict(self), "ready": self.ready})


@dataclass(frozen=True, slots=True)
class _HistoricalPlacementSelection:
    race_id: int
    result_id: int
    game_account_id: int
    persona_id: str
    race_business_key: str
    result_business_key: str
    game_account_business_key: str
    owner_business_key: str
    amount: int
    suppressed_result_business_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HistoricalPlacementCorrectionPlan:
    manifest: HistoricalPlacementCorrectionManifest
    selections: tuple[_HistoricalPlacementSelection, ...]


@dataclass(frozen=True, slots=True)
class HistoricalPlacementCorrectionApplyResult:
    import_run_id: int
    manifest_checksum: str
    expected_total: int
    created_transaction_count: int
    skipped_transaction_count: int
    already_applied: bool
    e2_prefix_state_unchanged: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _ResultContext:
    race: Race
    condition: RaceCondition
    entry: RaceEntry | None
    result: RaceResult
    source_record: SheetImportRecord | None
    attribution: LegacyIdentityMappingDecision | None


def build_historical_placement_correction_plan(
    session: Session,
    *,
    source_identifier: str,
    source_checksum: str,
    confirmed_mapping_decision_checksum: str,
    e2_business_key_prefix_checksum: str,
    mapping_loader: Callable[..., AppliedLegacyIdentityMappingAudit],
    prefix_runs_verifier: Callable[..., object],
    lock_rows: bool = False,
) -> HistoricalPlacementCorrectionPlan:
    """Build the deterministic Race 52-56 correction manifest without writes."""

    _require_clean_session(session)
    source = normalize_import_source_identifier(source_identifier)
    checksum = normalize_sha256_hex(source_checksum, field_name="source checksum")
    mapping_decision_checksum = normalize_sha256_hex(
        confirmed_mapping_decision_checksum,
        field_name="confirmed mapping decision checksum",
    )
    e2_prefix_checksum = normalize_sha256_hex(
        e2_business_key_prefix_checksum,
        field_name="E2 business-key prefix checksum",
    )
    mapping_audit = mapping_loader(
        session,
        source_identifier=source,
        source_checksum=checksum,
        confirmed_decision_checksum=mapping_decision_checksum,
        lock_rows=lock_rows,
    )
    prefix_runs_verifier(session, source_identifier=source, source_checksum=checksum, lock_rows=lock_rows)

    policy_checksum = _canonical_checksum(placement_reward_policy_basis())
    contexts, load_conflicts = _load_target_contexts(
        session,
        source_identifier=source,
        decisions=mapping_audit.decisions,
        lock_rows=lock_rows,
    )
    target_attributions = tuple(
        context.attribution for rows in contexts.values() for context in rows if context.attribution is not None
    )
    account_keys, owner_keys = _reviewed_owner_keys(
        source_identifier=source,
        attributions=target_attributions,
    )
    races, selections, allocation_conflicts = _allocate_manifest_races(
        session,
        source_identifier=source,
        contexts=contexts,
        account_keys=account_keys,
        owner_keys=owner_keys,
        lock_rows=lock_rows,
    )
    conflicts = tuple(
        sorted(
            (*load_conflicts, *allocation_conflicts),
            key=lambda row: (row.code, row.race_business_key or "", row.result_business_key or ""),
        )
    )
    target_attribution_checksum = _target_attribution_checksum(
        source_identifier=source,
        contexts=contexts,
        account_keys=account_keys,
        owner_keys=owner_keys,
    )
    per_owner: dict[str, int] = defaultdict(int)
    for selection in selections:
        per_owner[selection.owner_business_key] += selection.amount
    per_owner_deltas = tuple(
        {"owner_business_key": owner_key, "delta": per_owner[owner_key]} for owner_key in sorted(per_owner)
    )
    basis = {
        "manifest_version": HISTORICAL_PLACEMENT_CORRECTION_VERSION,
        "source_identifier": source,
        "source_checksum": checksum,
        "e2_business_key_prefix_checksum": e2_prefix_checksum,
        "mapping_checksum": mapping_audit.mapping_checksum,
        "mapping_decision_checksum": mapping_audit.decision_checksum,
        "target_attribution_checksum": target_attribution_checksum,
        "reward_policy_checksum": policy_checksum,
        "target_external_race_ids": HISTORICAL_PLACEMENT_TARGET_EXTERNAL_IDS,
        "races": [asdict(row) for row in races],
        "per_owner_deltas": per_owner_deltas,
        "expected_total": sum(selection.amount for selection in selections),
        "conflicts": [asdict(row) for row in conflicts],
    }
    manifest = HistoricalPlacementCorrectionManifest(
        manifest_version=HISTORICAL_PLACEMENT_CORRECTION_VERSION,
        source_identifier=source,
        source_checksum=checksum,
        e2_business_key_prefix_checksum=e2_prefix_checksum,
        mapping_checksum=mapping_audit.mapping_checksum,
        mapping_decision_checksum=mapping_audit.decision_checksum,
        target_attribution_checksum=target_attribution_checksum,
        reward_policy_checksum=policy_checksum,
        target_external_race_ids=HISTORICAL_PLACEMENT_TARGET_EXTERNAL_IDS,
        races=races,
        per_owner_deltas=per_owner_deltas,
        expected_total=sum(selection.amount for selection in selections),
        conflicts=conflicts,
        manifest_checksum=_canonical_checksum(basis),
    )
    return HistoricalPlacementCorrectionPlan(manifest=manifest, selections=selections)


def _load_target_contexts(
    session: Session,
    *,
    source_identifier: str,
    decisions: Sequence[LegacyIdentityMappingDecision],
    lock_rows: bool,
) -> tuple[dict[str, tuple[_ResultContext, ...]], tuple[HistoricalPlacementConflict, ...]]:
    decisions_by_source_key: dict[str, LegacyIdentityMappingDecision] = {}
    for decision in decisions:
        for source_key in decision.source_record_keys:
            if source_key in decisions_by_source_key:
                raise LegacyImportError("legacy identity mapping Result coverage is duplicated")
            decisions_by_source_key[source_key] = decision

    race_statement = select(Race).where(
        Race.external_source == source_identifier,
        Race.race_kind == "room_match",
        Race.external_race_id.in_(HISTORICAL_PLACEMENT_TARGET_EXTERNAL_IDS),
    )
    if lock_rows:
        race_statement = race_statement.with_for_update()
    races = tuple(session.scalars(race_statement))
    races_by_external_id = {race.external_race_id: race for race in races}
    conflicts: list[HistoricalPlacementConflict] = []
    for external_id in HISTORICAL_PLACEMENT_TARGET_EXTERNAL_IDS:
        if external_id not in races_by_external_id:
            conflicts.append(
                HistoricalPlacementConflict(
                    code="target_race_missing",
                    race_business_key=_race_business_key(source_identifier, external_id),
                )
            )
    if not races:
        return {}, tuple(conflicts)

    race_ids = [race.id for race in races]
    condition_statement = select(RaceCondition).where(RaceCondition.race_id.in_(race_ids))
    entry_statement = select(RaceEntry).where(RaceEntry.race_id.in_(race_ids))
    result_statement = select(RaceResult).where(RaceResult.race_id.in_(race_ids))
    if lock_rows:
        condition_statement = condition_statement.with_for_update()
        entry_statement = entry_statement.with_for_update()
        result_statement = result_statement.with_for_update()
    conditions = {row.race_id: row for row in session.scalars(condition_statement)}
    entries = tuple(session.scalars(entry_statement))
    results = tuple(session.scalars(result_statement))
    entries_by_key = {(row.race_id, row.entry_number): row for row in entries}
    record_ids = [row.source_import_record_id for row in results if row.source_import_record_id is not None]
    record_statement = select(SheetImportRecord).where(SheetImportRecord.id.in_(record_ids))
    if lock_rows:
        record_statement = record_statement.with_for_update()
    records = {row.id: row for row in session.scalars(record_statement)}
    output: dict[str, tuple[_ResultContext, ...]] = {}
    for external_id in HISTORICAL_PLACEMENT_TARGET_EXTERNAL_IDS:
        race = races_by_external_id.get(external_id)
        if race is None:
            continue
        condition = conditions.get(race.id)
        race_key = _race_business_key(source_identifier, external_id)
        if condition is None:
            conflicts.append(HistoricalPlacementConflict(code="race_condition_missing", race_business_key=race_key))
            continue
        race_results = sorted(
            (row for row in results if row.race_id == race.id),
            key=lambda row: row.entry_number,
        )
        output[external_id] = tuple(
            _ResultContext(
                race=race,
                condition=condition,
                entry=entries_by_key.get((race.id, result.entry_number)),
                result=result,
                source_record=(
                    records.get(result.source_import_record_id) if result.source_import_record_id is not None else None
                ),
                attribution=(
                    decisions_by_source_key.get(records[result.source_import_record_id].source_key)
                    if result.source_import_record_id in records
                    else None
                ),
            )
            for result in race_results
        )
    return output, tuple(conflicts)


def _allocate_manifest_races(
    session: Session,
    *,
    source_identifier: str,
    contexts: Mapping[str, tuple[_ResultContext, ...]],
    account_keys: Mapping[int, str],
    owner_keys: Mapping[str, str],
    lock_rows: bool,
) -> tuple[
    tuple[HistoricalPlacementRaceManifestRow, ...],
    tuple[_HistoricalPlacementSelection, ...],
    tuple[HistoricalPlacementConflict, ...],
]:
    account_ids = sorted(
        {
            context.attribution.game_account_id
            for rows in contexts.values()
            for context in rows
            if context.attribution is not None and context.attribution.game_account_id is not None
        }
    )
    persona_ids = sorted(
        {
            context.attribution.persona_id
            for rows in contexts.values()
            for context in rows
            if context.attribution is not None and context.attribution.persona_id is not None
        }
    )
    account_statement = select(GameAccount).where(GameAccount.id.in_(account_ids))
    wallet_statement = select(CirclePointAccount).where(CirclePointAccount.persona_id.in_(persona_ids))
    if lock_rows:
        account_statement = account_statement.with_for_update()
        wallet_statement = wallet_statement.with_for_update()
    accounts = {row.id: row for row in session.scalars(account_statement)}
    wallets = {row.persona_id: row for row in session.scalars(wallet_statement)}

    manifest_races: list[HistoricalPlacementRaceManifestRow] = []
    persisted_selections: list[_HistoricalPlacementSelection] = []
    conflicts: list[HistoricalPlacementConflict] = []
    for external_id in HISTORICAL_PLACEMENT_TARGET_EXTERNAL_IDS:
        race_contexts = contexts.get(external_id)
        if race_contexts is None:
            continue
        first = race_contexts[0] if race_contexts else None
        race_key = _race_business_key(source_identifier, external_id)
        if first is None:
            conflicts.append(
                HistoricalPlacementConflict(code="target_race_results_missing", race_business_key=race_key)
            )
            continue
        condition = first.condition
        entries = [context.entry for context in race_contexts if context.entry is not None]
        if len(entries) != condition.participant_count or len(race_contexts) != condition.participant_count:
            conflicts.append(HistoricalPlacementConflict(code="participant_count_mismatch", race_business_key=race_key))
        if {context.result.rank for context in race_contexts} != set(range(1, len(race_contexts) + 1)):
            conflicts.append(
                HistoricalPlacementConflict(code="result_rank_coverage_mismatch", race_business_key=race_key)
            )

        rows_by_key: dict[str, HistoricalPlacementResultManifestRow] = {}
        internal_by_key: dict[str, tuple[_ResultContext, str, str]] = {}
        candidates: list[MatchPlacementCandidate] = []
        for context in race_contexts:
            fallback_key = _canonical_checksum(
                {"race_business_key": race_key, "entry_number": context.result.entry_number}
            )
            record = context.source_record
            result_key = record.source_key if record is not None else fallback_key
            attribution = context.attribution
            attribution_status = _attribution_status(attribution)
            account_key = (
                account_keys.get(attribution.game_account_id)
                if attribution is not None and attribution.game_account_id is not None
                else None
            )
            owner_key = (
                owner_keys.get(attribution.persona_id)
                if attribution is not None and attribution.persona_id is not None
                else None
            )
            outcome = "candidate"
            if (
                record is None
                or record.status != "applied"
                or record.target_entity_type != "race_result"
                or record.target_entity_id != context.result.id
                or attribution is None
                or result_key not in attribution.source_record_keys
            ):
                conflicts.append(
                    HistoricalPlacementConflict(
                        code="result_source_provenance_missing",
                        race_business_key=race_key,
                        result_business_key=result_key,
                    )
                )
                outcome = "conflict"
            elif attribution.disposition == UNRESOLVED_HISTORY:
                conflicts.append(
                    HistoricalPlacementConflict(
                        code="unresolved_history",
                        race_business_key=race_key,
                        result_business_key=result_key,
                    )
                )
                outcome = UNRESOLVED_HISTORY
            elif attribution.disposition == HISTORY_CONTEXT_ONLY:
                if any(
                    value is not None
                    for value in (
                        context.entry.game_account_id if context.entry is not None else None,
                        context.entry.owner_at_event_persona_id if context.entry is not None else None,
                        context.result.game_account_id,
                        context.result.owner_at_event_persona_id,
                    )
                ):
                    conflicts.append(
                        HistoricalPlacementConflict(
                            code="history_context_only_owner_changed",
                            race_business_key=race_key,
                            result_business_key=result_key,
                        )
                    )
                    outcome = "conflict"
                else:
                    outcome = HISTORY_CONTEXT_ONLY
            elif attribution.disposition != RATING_REPLAY:
                conflicts.append(
                    HistoricalPlacementConflict(
                        code="unsupported_history_disposition",
                        race_business_key=race_key,
                        result_business_key=result_key,
                    )
                )
                outcome = "conflict"
            elif context.result.is_result_void:
                outcome = "no_reward_void"
            elif condition.grade.strip().upper() == "OP":
                outcome = "no_reward_op"
            else:
                ownership_errors = _candidate_ownership_errors(
                    context,
                    attribution=attribution,
                    accounts=accounts,
                    wallets=wallets,
                    account_key=account_key,
                    owner_key=owner_key,
                )
                for code in ownership_errors:
                    conflicts.append(
                        HistoricalPlacementConflict(
                            code=code,
                            race_business_key=race_key,
                            result_business_key=result_key,
                        )
                    )
                if ownership_errors:
                    outcome = "conflict"
                else:
                    assert account_key is not None and owner_key is not None
                    candidates.append(
                        MatchPlacementCandidate(
                            result_business_key=result_key,
                            game_account_business_key=account_key,
                            owner_business_key=owner_key,
                            entry_number=context.result.entry_number,
                            rank=context.result.rank,
                        )
                    )
                    internal_by_key[result_key] = (context, account_key, owner_key)

            rows_by_key[result_key] = HistoricalPlacementResultManifestRow(
                result_business_key=result_key,
                entry_number=context.result.entry_number,
                rank=context.result.rank,
                attribution_status=attribution_status,
                is_rating_excluded=context.result.is_rating_excluded,
                is_result_void=context.result.is_result_void,
                game_account_business_key=account_key,
                owner_business_key=owner_key,
                outcome=outcome,
                amount=0,
            )

        try:
            selections = allocate_match_placement_rewards(
                candidates,
                grade=condition.grade,
                participant_count=condition.participant_count,
            )
        except MatchPlacementRewardError:
            selections = ()
            conflicts.append(HistoricalPlacementConflict(code="unsupported_reward_policy", race_business_key=race_key))
        for selection in selections:
            selected_key = selection.candidate.result_business_key
            rows_by_key[selected_key] = replace(
                rows_by_key[selected_key],
                outcome="selected",
                amount=selection.amount,
            )
            context, account_key, owner_key = internal_by_key[selected_key]
            attribution = context.attribution
            assert attribution is not None
            assert attribution.game_account_id is not None
            assert attribution.persona_id is not None
            persisted_selections.append(
                _HistoricalPlacementSelection(
                    race_id=context.race.id,
                    result_id=context.result.id,
                    game_account_id=attribution.game_account_id,
                    persona_id=attribution.persona_id,
                    race_business_key=race_key,
                    result_business_key=selected_key,
                    game_account_business_key=account_key,
                    owner_business_key=owner_key,
                    amount=selection.amount,
                    suppressed_result_business_keys=selection.suppressed_result_business_keys,
                )
            )
            for suppressed_key in selection.suppressed_result_business_keys:
                rows_by_key[suppressed_key] = replace(
                    rows_by_key[suppressed_key],
                    outcome="suppressed_by_persona_cap",
                    selected_result_business_key=selected_key,
                )
        ordered_rows = tuple(sorted(rows_by_key.values(), key=lambda row: (row.entry_number, row.result_business_key)))
        manifest_races.append(
            HistoricalPlacementRaceManifestRow(
                race_business_key=race_key,
                external_race_id=external_id,
                grade=condition.grade.strip().upper(),
                participant_count=condition.participant_count,
                result_rows=ordered_rows,
                delta=sum(row.amount for row in ordered_rows),
            )
        )
    return (
        tuple(manifest_races),
        tuple(sorted(persisted_selections, key=lambda row: row.result_business_key)),
        tuple(conflicts),
    )


def _candidate_ownership_errors(
    context: _ResultContext,
    *,
    attribution: LegacyIdentityMappingDecision,
    accounts: Mapping[int, GameAccount],
    wallets: Mapping[str, CirclePointAccount],
    account_key: str | None,
    owner_key: str | None,
) -> tuple[str, ...]:
    errors: list[str] = []
    entry = context.entry
    if entry is None:
        errors.append("matching_entry_missing")
    if attribution.game_account_id is None or attribution.game_account_id not in accounts or account_key is None:
        errors.append("game_account_unresolved")
    if attribution.persona_id is None or owner_key is None:
        errors.append("owner_at_event_unresolved")
    elif attribution.persona_id not in wallets:
        errors.append("owner_wallet_missing")
    if entry is not None and (
        entry.game_account_id != attribution.game_account_id
        or context.result.game_account_id != attribution.game_account_id
    ):
        errors.append("entry_result_game_account_mismatch")
    if entry is not None and (
        entry.owner_at_event_persona_id != attribution.persona_id
        or context.result.owner_at_event_persona_id != attribution.persona_id
    ):
        errors.append("entry_result_owner_at_event_mismatch")
    return tuple(dict.fromkeys(errors))


def _reviewed_owner_keys(
    *,
    source_identifier: str,
    attributions: Sequence[LegacyIdentityMappingDecision],
) -> tuple[dict[int, str], dict[str, str]]:
    chains_by_account: dict[int, set[str]] = defaultdict(set)
    chains_by_owner: dict[str, set[str]] = defaultdict(set)
    for row in attributions:
        if row.disposition != RATING_REPLAY:
            continue
        if row.game_account_id is not None:
            chains_by_account[row.game_account_id].add(row.source_chain_key)
        if row.persona_id is not None:
            chains_by_owner[row.persona_id].add(row.source_chain_key)
    account_keys = {
        account_id: _canonical_checksum(
            {
                "kind": "reviewed_game_account_mapping",
                "source_identifier": source_identifier,
                "source_chain_keys": sorted(chain_keys),
            }
        )
        for account_id, chain_keys in chains_by_account.items()
    }
    owner_keys = {
        persona_id: _canonical_checksum(
            {
                "kind": "reviewed_owner_at_event_mapping",
                "source_identifier": source_identifier,
                "source_chain_keys": sorted(chain_keys),
            }
        )
        for persona_id, chain_keys in chains_by_owner.items()
    }
    return account_keys, owner_keys


def _target_attribution_checksum(
    *,
    source_identifier: str,
    contexts: Mapping[str, tuple[_ResultContext, ...]],
    account_keys: Mapping[int, str],
    owner_keys: Mapping[str, str],
) -> str:
    basis: list[dict[str, object]] = []
    for external_id in HISTORICAL_PLACEMENT_TARGET_EXTERNAL_IDS:
        race_key = _race_business_key(source_identifier, external_id)
        for context in contexts.get(external_id, ()):
            attribution = context.attribution
            record = context.source_record
            result_key = (
                record.source_key
                if record is not None
                else _canonical_checksum(
                    {
                        "race_business_key": race_key,
                        "entry_number": context.result.entry_number,
                    }
                )
            )
            basis.append(
                {
                    "race_business_key": race_key,
                    "result_business_key": result_key,
                    "entry_number": context.result.entry_number,
                    "source_chain_key": attribution.source_chain_key if attribution is not None else None,
                    "attribution_status": _attribution_status(attribution),
                    "game_account_business_key": (
                        account_keys.get(attribution.game_account_id)
                        if attribution is not None and attribution.game_account_id is not None
                        else None
                    ),
                    "owner_business_key": (
                        owner_keys.get(attribution.persona_id)
                        if attribution is not None and attribution.persona_id is not None
                        else None
                    ),
                }
            )
    return _canonical_checksum(basis)


def _attribution_status(attribution: LegacyIdentityMappingDecision | None) -> str:
    if attribution is None:
        return "missing"
    if attribution.disposition == RATING_REPLAY:
        return "reviewed_owner"
    return attribution.disposition


def _race_business_key(source_identifier: str, external_race_id: str) -> str:
    return _canonical_checksum(
        {
            "kind": "legacy_room_match_race",
            "source_identifier": source_identifier,
            "external_race_id": external_race_id,
        }
    )


def _canonical_checksum(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        ).encode("utf-8")
    ).hexdigest()


def _json_default(value: object) -> object:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _json_value(value: object) -> dict[str, object]:
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        )
    )


def _require_clean_session(session: Session) -> None:
    if session.new or session.dirty or session.deleted:
        raise LegacyImportError("historical placement correction requires a clean session")
