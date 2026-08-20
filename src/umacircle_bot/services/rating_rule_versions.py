from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from hashlib import sha256

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import RATING_NUMERIC_SCALE, RatingRule, RatingRuleVersion
from umacircle_bot.domain.errors import LegacyImportConflictError, LegacyImportError
from umacircle_bot.domain.imports import (
    normalize_import_sheet_name,
    normalize_import_source_identifier,
    normalize_sha256_hex,
)
from umacircle_bot.sheets.rating_rule_workbook import RatingRuleWorkbookPreparation

RATING_STORAGE_QUANTUM = Decimal(1).scaleb(-RATING_NUMERIC_SCALE)


@dataclass(frozen=True)
class RatingRuleSeedPlan:
    source_identifier: str
    source_checksum: str
    source_sheet_name: str
    source_range: str
    rule_set_checksum: str
    rules: tuple[tuple[str, int, int, Decimal], ...]


@dataclass(frozen=True)
class RatingRuleSeedPreview:
    existing_version_number: int | None
    next_version_number: int
    rule_count: int

    @property
    def can_apply(self) -> bool:
        return self.existing_version_number is None


@dataclass(frozen=True)
class RatingRuleSeedResult:
    version_id: int
    version_number: int
    rule_count: int
    created: bool


def build_rating_rule_seed_plan(
    preparation: RatingRuleWorkbookPreparation,
    *,
    source_identifier: str,
) -> RatingRuleSeedPlan:
    normalized_rules = tuple(
        sorted(
            (
                rule.grade.strip().upper(),
                rule.participant_count,
                rule.converted_rank,
                _normalize_rating_value(rule.base_delta),
            )
            for rule in preparation.rules
        )
    )
    if not normalized_rules:
        raise LegacyImportError("rating rule seed requires at least one rule")
    if len({rule[:3] for rule in normalized_rules}) != len(normalized_rules):
        raise LegacyImportError("rating rule seed contains duplicate grade/participant/rank rules")
    canonical_rules = [[grade, count, rank, format(delta, "f")] for grade, count, rank, delta in normalized_rules]
    return RatingRuleSeedPlan(
        source_identifier=normalize_import_source_identifier(source_identifier),
        source_checksum=normalize_sha256_hex(preparation.source_checksum, field_name="source checksum"),
        source_sheet_name=normalize_import_sheet_name(preparation.sheet_name),
        source_range=_normalize_source_range(preparation.source_range),
        rule_set_checksum=sha256(
            json.dumps(canonical_rules, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        rules=normalized_rules,
    )


def preview_rating_rule_seed(session: Session, *, plan: RatingRuleSeedPlan) -> RatingRuleSeedPreview:
    _require_clean_session(session)
    existing = _existing_version(session, plan=plan, lock_rows=False)
    latest = session.scalar(select(func.max(RatingRuleVersion.version_number))) or 0
    return RatingRuleSeedPreview(
        existing_version_number=existing.version_number if existing is not None else None,
        next_version_number=latest + 1,
        rule_count=len(plan.rules),
    )


def apply_rating_rule_seed(session: Session, *, plan: RatingRuleSeedPlan) -> RatingRuleSeedResult:
    _require_clean_session(session)
    with session.begin_nested():
        existing = _existing_version(session, plan=plan, lock_rows=True)
        if existing is not None:
            if existing.rule_set_checksum != plan.rule_set_checksum:
                raise LegacyImportConflictError("existing RatingRule source provenance has different rule content")
            return RatingRuleSeedResult(
                version_id=existing.id,
                version_number=existing.version_number,
                rule_count=existing.rule_count,
                created=False,
            )
        latest = session.scalar(select(func.max(RatingRuleVersion.version_number)).with_for_update()) or 0
        version = RatingRuleVersion(
            version_number=latest + 1,
            source_identifier=plan.source_identifier,
            source_checksum=plan.source_checksum,
            source_sheet_name=plan.source_sheet_name,
            source_range=plan.source_range,
            rule_set_checksum=plan.rule_set_checksum,
            rule_count=len(plan.rules),
        )
        session.add(version)
        session.flush()
        session.add_all(
            RatingRule(
                rating_rule_version_id=version.id,
                grade=grade,
                participant_count=participant_count,
                converted_rank=converted_rank,
                base_delta=base_delta,
            )
            for grade, participant_count, converted_rank, base_delta in plan.rules
        )
    return RatingRuleSeedResult(
        version_id=version.id,
        version_number=version.version_number,
        rule_count=version.rule_count,
        created=True,
    )


def _existing_version(session: Session, *, plan: RatingRuleSeedPlan, lock_rows: bool) -> RatingRuleVersion | None:
    statement = select(RatingRuleVersion).where(
        RatingRuleVersion.source_identifier == plan.source_identifier,
        RatingRuleVersion.source_checksum == plan.source_checksum,
        RatingRuleVersion.source_sheet_name == plan.source_sheet_name,
        RatingRuleVersion.source_range == plan.source_range,
    )
    if lock_rows:
        statement = statement.with_for_update()
    return session.scalar(statement)


def _normalize_rating_value(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal):
        raise LegacyImportError("rating rule base delta must be Decimal")
    normalized = value.quantize(RATING_STORAGE_QUANTUM, rounding=ROUND_HALF_EVEN)
    if normalized.adjusted() > 11:
        raise LegacyImportError("rating rule base delta exceeds NUMERIC(30,18)")
    return normalized


def _normalize_source_range(value: str) -> str:
    if not isinstance(value, str):
        raise LegacyImportError("rating rule source range must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > 64 or any(ord(character) < 32 for character in normalized):
        raise LegacyImportError("rating rule source range must contain 1 to 64 printable characters")
    return normalized


def _require_clean_session(session: Session) -> None:
    if session.new or session.dirty or session.deleted:
        raise LegacyImportError("RatingRule seed requires a clean session")
