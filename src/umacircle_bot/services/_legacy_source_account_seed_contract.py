from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from umacircle_bot.domain.errors import LegacyImportError
from umacircle_bot.domain.imports import normalize_import_source_identifier, normalize_sha256_hex

SOURCE_ACCOUNT_SEED_MANIFEST_VERSION = 1
LEGACY_SOURCE_ACCOUNT_SEED_IMPORT_KIND = "legacy_room_source_accounts"
LEGACY_SOURCE_ACCOUNT_SEED_RECORD_TYPE = "legacy_source_account_decision"
LEGACY_SOURCE_ACCOUNT_SEED_SOURCE_TYPE = "operator_manifest"
LEGACY_SOURCE_ACCOUNT_SEED_SHEET_NAME = "source_account_seed"
CREATE_SOURCE_ONLY = "create_source_only"
NO_SEED = "no_seed"
SOURCE_ACCOUNT_SEED_ACTIONS = frozenset({CREATE_SOURCE_ONLY, NO_SEED})


@dataclass(frozen=True, slots=True)
class LegacySourceAccountSeedPreview:
    source_identifier: str
    source_checksum: str
    seed_basis_checksum: str
    decision_checksum: str
    decision_count: int
    create_count: int
    no_seed_count: int
    already_applied: bool


@dataclass(frozen=True, slots=True)
class LegacySourceAccountSeedApplyResult:
    import_run_id: int
    source_identifier: str
    source_checksum: str
    seed_basis_checksum: str
    decision_checksum: str
    decision_count: int
    created_count: int
    no_seed_count: int
    exact_retry: bool


@dataclass(frozen=True, slots=True)
class AppliedLegacySourceAccountSeed:
    import_run_id: int
    source_identifier: str
    source_checksum: str
    seed_basis_checksum: str
    decision_checksum: str
    decision_count: int
    created_game_account_ids: tuple[int, ...]
    no_seed_count: int


@dataclass(frozen=True, slots=True)
class _SeedDecision:
    normalized_player_name: str
    source_chain_key: str
    source_player_names: tuple[str, ...]
    source_record_keys: tuple[str, ...]
    occurrence_count: int
    rating_eligible_occurrence_count: int
    exact_candidate_game_account_ids: tuple[int, ...]
    status: str
    action: str
    game_account_display_name: str | None
    operator_note: str


@dataclass(frozen=True, slots=True)
class _SeedPlan:
    source_identifier: str
    source_checksum: str
    seed_basis_checksum: str
    decision_checksum: str
    reviewed_by: tuple[str, ...]
    reviewed_at: str
    evidence_note: str | None
    decisions: tuple[_SeedDecision, ...]

    @property
    def create_count(self) -> int:
        return sum(decision.action == CREATE_SOURCE_ONLY for decision in self.decisions)

    @property
    def no_seed_count(self) -> int:
        return sum(decision.action == NO_SEED for decision in self.decisions)


def _build_submitted_plan(
    decision_manifest: Mapping[str, object],
    *,
    live_template: Mapping[str, object] | None = None,
) -> _SeedPlan:
    source_identifier, source_checksum = _manifest_identity(decision_manifest)
    if decision_manifest.get("manifest_version") != SOURCE_ACCOUNT_SEED_MANIFEST_VERSION:
        raise LegacyImportError("legacy source-account seed manifest version is unsupported")
    seed_basis_checksum = normalize_sha256_hex(
        _required_text(decision_manifest.get("seed_basis_checksum"), field="seed basis checksum"),
        field_name="seed basis checksum",
    )
    submitted_candidates = decision_manifest.get("candidates")
    if not isinstance(submitted_candidates, (list, tuple)) or not submitted_candidates:
        raise LegacyImportError("legacy source-account seed requires candidate decisions")
    decisions = tuple(
        sorted(
            (_parse_decision(row) for row in submitted_candidates),
            key=lambda row: row.normalized_player_name,
        )
    )
    if len({decision.normalized_player_name for decision in decisions}) != len(decisions):
        raise LegacyImportError("legacy source-account seed contains duplicate source names")

    if live_template is not None:
        if live_template.get("seed_basis_checksum") != seed_basis_checksum:
            raise LegacyImportError("legacy source-account seed source or candidate state changed after review")
        live_candidates = {
            str(candidate["normalized_player_name"]): _candidate_basis(candidate)
            for candidate in live_template["candidates"]
            if isinstance(candidate, Mapping)
        }
        submitted_basis = {decision.normalized_player_name: _decision_basis(decision) for decision in decisions}
        if submitted_basis != live_candidates:
            raise LegacyImportError("legacy source-account seed decisions do not match the review template")

    review = decision_manifest.get("review")
    if not isinstance(review, Mapping):
        raise LegacyImportError("legacy source-account seed requires a two-reviewer record")
    reviewer_values = review.get("reviewed_by")
    if not isinstance(reviewer_values, (list, tuple)):
        raise LegacyImportError("legacy source-account seed requires two reviewers")
    reviewed_by = tuple(sorted({_required_text(value, field="reviewer") for value in reviewer_values}))
    if len(reviewed_by) < 2:
        raise LegacyImportError("legacy source-account seed requires two distinct reviewers")
    reviewed_at = _required_text(review.get("reviewed_at"), field="reviewed at")
    try:
        parsed_reviewed_at = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError:
        raise LegacyImportError("legacy source-account seed reviewed_at must be ISO-8601") from None
    if parsed_reviewed_at.tzinfo is None:
        raise LegacyImportError("legacy source-account seed reviewed_at must include a timezone")
    evidence_note = _optional_text(review.get("evidence_note"), field="evidence note", maximum=2000)
    return _SeedPlan(
        source_identifier=source_identifier,
        source_checksum=source_checksum,
        seed_basis_checksum=seed_basis_checksum,
        decision_checksum=_decision_checksum(
            source_identifier=source_identifier,
            source_checksum=source_checksum,
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


def _parse_decision(value: object) -> _SeedDecision:
    if not isinstance(value, Mapping):
        raise LegacyImportError("legacy source-account seed candidate must be an object")
    normalized_name = _required_text(value.get("normalized_player_name"), field="normalized player name")
    source_chain_key = normalize_sha256_hex(
        _required_text(value.get("source_chain_key"), field=f"source chain key for {normalized_name}"),
        field_name=f"source chain key for {normalized_name}",
    )
    source_player_names = _text_tuple(
        value.get("source_player_names"),
        field=f"source player names for {normalized_name}",
        maximum=100,
    )
    source_record_keys = tuple(
        normalize_sha256_hex(item, field_name=f"source record key for {normalized_name}")
        for item in _text_tuple(
            value.get("source_record_keys"),
            field=f"source record keys for {normalized_name}",
            maximum=64,
        )
    )
    occurrence_count = _positive_int(value.get("occurrence_count"), field=f"occurrence count for {normalized_name}")
    rating_count = _nonnegative_int(
        value.get("rating_eligible_occurrence_count"),
        field=f"Rating occurrence count for {normalized_name}",
    )
    candidate_ids = _positive_int_tuple(
        value.get("exact_candidate_game_account_ids"),
        field=f"exact candidate IDs for {normalized_name}",
    )
    status = _required_text(value.get("status"), field=f"status for {normalized_name}")
    decision = value.get("decision")
    if not isinstance(decision, Mapping):
        raise LegacyImportError(f"legacy source-account seed decision is missing: {normalized_name}")
    action = _required_text(decision.get("action"), field=f"action for {normalized_name}")
    if action not in SOURCE_ACCOUNT_SEED_ACTIONS:
        raise LegacyImportError(f"legacy source-account seed action is unsupported: {normalized_name}")
    display_name = _optional_text(
        decision.get("game_account_display_name"),
        field=f"GameAccount display name for {normalized_name}",
        maximum=100,
    )
    note = _optional_text(
        decision.get("operator_note"),
        field=f"operator note for {normalized_name}",
        maximum=1000,
    )
    if note is None:
        raise LegacyImportError(f"legacy source-account seed decision requires an operator note: {normalized_name}")
    if action == CREATE_SOURCE_ONLY and display_name is None:
        raise LegacyImportError(f"source-only GameAccount display name is required: {normalized_name}")
    if action == NO_SEED and display_name is not None:
        raise LegacyImportError(f"no-seed decision cannot define a GameAccount display name: {normalized_name}")
    return _SeedDecision(
        normalized_player_name=normalized_name,
        source_chain_key=source_chain_key,
        source_player_names=source_player_names,
        source_record_keys=source_record_keys,
        occurrence_count=occurrence_count,
        rating_eligible_occurrence_count=rating_count,
        exact_candidate_game_account_ids=candidate_ids,
        status=status,
        action=action,
        game_account_display_name=display_name,
        operator_note=note,
    )


def _manifest_identity(decision_manifest: Mapping[str, object]) -> tuple[str, str]:
    if not isinstance(decision_manifest, Mapping):
        raise LegacyImportError("legacy source-account seed decision must be a JSON object")
    source_identifier = normalize_import_source_identifier(
        _required_text(decision_manifest.get("source_identifier"), field="source identifier")
    )
    source_checksum = normalize_sha256_hex(
        _required_text(decision_manifest.get("source_checksum"), field="source checksum"),
        field_name="source checksum",
    )
    return source_identifier, source_checksum


def _candidate_basis(candidate: Mapping[str, object]) -> dict[str, object]:
    return {
        "normalized_player_name": candidate["normalized_player_name"],
        "source_chain_key": candidate["source_chain_key"],
        "source_player_names": list(candidate["source_player_names"]),
        "source_record_keys": list(candidate["source_record_keys"]),
        "occurrence_count": candidate["occurrence_count"],
        "rating_eligible_occurrence_count": candidate["rating_eligible_occurrence_count"],
        "exact_candidate_game_account_ids": list(candidate["exact_candidate_game_account_ids"]),
        "status": candidate["status"],
    }


def _decision_basis(decision: _SeedDecision) -> dict[str, object]:
    return {
        "normalized_player_name": decision.normalized_player_name,
        "source_chain_key": decision.source_chain_key,
        "source_player_names": list(decision.source_player_names),
        "source_record_keys": list(decision.source_record_keys),
        "occurrence_count": decision.occurrence_count,
        "rating_eligible_occurrence_count": decision.rating_eligible_occurrence_count,
        "exact_candidate_game_account_ids": list(decision.exact_candidate_game_account_ids),
        "status": decision.status,
    }


def _record_source_key(*, plan: _SeedPlan, decision: _SeedDecision) -> str:
    return sha256(
        (
            f"{LEGACY_SOURCE_ACCOUNT_SEED_IMPORT_KIND}\0{plan.source_identifier}\0"
            f"{plan.decision_checksum}\0{decision.normalized_player_name}"
        ).encode()
    ).hexdigest()


def _record_fingerprint(decision: _SeedDecision) -> str:
    return _checksum(
        {
            **_decision_basis(decision),
            "action": decision.action,
            "game_account_display_name": decision.game_account_display_name,
            "operator_note": decision.operator_note,
        }
    )


def _record_detail(decision: _SeedDecision, *, plan: _SeedPlan) -> dict[str, object]:
    return {
        "manifest_version": SOURCE_ACCOUNT_SEED_MANIFEST_VERSION,
        "source_checksum": plan.source_checksum,
        "seed_basis_checksum": plan.seed_basis_checksum,
        "decision_checksum": plan.decision_checksum,
        **_decision_basis(decision),
        "action": decision.action,
        "game_account_display_name": decision.game_account_display_name,
        "operator_note": decision.operator_note,
    }


def _run_summary(plan: _SeedPlan, *, created_count: int) -> dict[str, object]:
    return {
        "manifest_version": SOURCE_ACCOUNT_SEED_MANIFEST_VERSION,
        "source_checksum": plan.source_checksum,
        "seed_basis_checksum": plan.seed_basis_checksum,
        "decision_checksum": plan.decision_checksum,
        "decision_count": len(plan.decisions),
        "create_count": plan.create_count,
        "created_count": created_count,
        "no_seed_count": plan.no_seed_count,
        "reviewed_by": list(plan.reviewed_by),
        "reviewed_at": plan.reviewed_at,
        "evidence_note": plan.evidence_note,
    }


def _decision_checksum(
    *,
    source_identifier: str,
    source_checksum: str,
    seed_basis_checksum: str,
    reviewed_by: tuple[str, ...],
    reviewed_at: str,
    evidence_note: str | None,
    decisions: tuple[_SeedDecision, ...],
) -> str:
    return _checksum(
        {
            "manifest_version": SOURCE_ACCOUNT_SEED_MANIFEST_VERSION,
            "source_identifier": source_identifier,
            "source_checksum": source_checksum,
            "seed_basis_checksum": seed_basis_checksum,
            "reviewed_by": reviewed_by,
            "reviewed_at": reviewed_at,
            "evidence_note": evidence_note,
            "decisions": [
                {
                    **_decision_basis(decision),
                    "action": decision.action,
                    "game_account_display_name": decision.game_account_display_name,
                    "operator_note": decision.operator_note,
                }
                for decision in decisions
            ],
        }
    )


def _checksum(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _text_tuple(value: object, *, field: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise LegacyImportError(f"legacy source-account seed {field} must be a non-empty list")
    items = tuple(_required_text(item, field=field, maximum=maximum) for item in value)
    if len(set(items)) != len(items):
        raise LegacyImportError(f"legacy source-account seed {field} contains duplicates")
    return items


def _positive_int_tuple(value: object, *, field: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        raise LegacyImportError(f"legacy source-account seed {field} must be a list")
    items = tuple(_positive_int(item, field=field) for item in value)
    if len(set(items)) != len(items):
        raise LegacyImportError(f"legacy source-account seed {field} contains duplicates")
    return items


def _positive_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise LegacyImportError(f"legacy source-account seed {field} must be a positive integer")
    return value


def _nonnegative_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise LegacyImportError(f"legacy source-account seed {field} must be a nonnegative integer")
    return value


def _required_text(value: object, *, field: str, maximum: int = 200) -> str:
    if not isinstance(value, str):
        raise LegacyImportError(f"legacy source-account seed {field} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise LegacyImportError(f"legacy source-account seed {field} is invalid")
    return normalized


def _optional_text(value: object, *, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LegacyImportError(f"legacy source-account seed {field} must be text")
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > maximum:
        raise LegacyImportError(f"legacy source-account seed {field} is too long")
    return normalized
