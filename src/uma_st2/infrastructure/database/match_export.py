"""SQLAlchemy projection reader for complete Circle Match Season exports."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from uma_st2.application.betting import BetReplacementAuditType, ReplacedMatchBet
from uma_st2.application.exporting import (
    MatchExportBet,
    MatchExportCondition,
    MatchExportCourse,
    MatchExportEntry,
    MatchExportMatch,
    MatchExportRatingTransaction,
    MatchExportSeason,
    MatchExportSeasonChoice,
    MatchSeasonExportSource,
    match_export_season_for_datetime,
    parse_match_export_season,
)
from uma_st2.application.match import (
    MATCH_BET_PAYOUT_REVERSAL_POINT_ACTION,
    MATCH_BET_REFUND_POINT_ACTION,
    CancelledMatch,
    MatchCancellationAuditType,
    MatchSettlementAuditType,
    MatchSettlementRollbackAuditType,
    RolledBackMatchSettlement,
    SettledMatch,
)
from uma_st2.domain.betting import BetStatus, BetType
from uma_st2.domain.identity import GameRegion
from uma_st2.domain.match import (
    MatchDirection,
    MatchGrade,
    MatchRatingDisposition,
    MatchSeason,
    MatchSourceKind,
    MatchStatus,
    MatchSurface,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
    StadiumCourseLayout,
)

from .datetime_codec import from_database_utc, to_database_utc
from .orm import (
    BetOperationORM,
    BetORM,
    GameAccountORM,
    MatchConditionORM,
    MatchEntryORM,
    MatchOperationORM,
    MatchORM,
    PersonaORM,
    RatingRuleVersionORM,
    RatingTransactionORM,
    StadiumCourseORM,
    StadiumORM,
    UmamusumeORM,
    UmamusumeVariantORM,
)
from .uow import SessionFactory, SqlAlchemyFeatureUnitOfWork, SqlAlchemyFeatureUnitOfWorkFactory


@dataclass(slots=True)
class _BetEvidence:
    applied_odds: Decimal | None = None
    payout_amount: int | None = None
    payout_group_bet_ids: tuple[int, ...] = field(default_factory=tuple)
    payout_reversal_amount: int | None = None
    refund_amount: int | None = None
    refund_group_bet_ids: tuple[int, ...] = field(default_factory=tuple)
    refund_reason: str | None = None
    refunded_at: datetime | None = None


def _display_name(primary: str | None, fallback: str) -> str:
    return primary.strip() if primary is not None and primary.strip() else fallback


def _optional_utc(value: datetime | None, *, field_name: str) -> datetime | None:
    return None if value is None else from_database_utc(value, field_name=field_name)


class SqlAlchemyMatchSeasonExportRepository:
    """Materialize complete detached Circle Match Season export sources."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def search_seasons(self, *, query: str, limit: int) -> tuple[MatchExportSeasonChoice, ...]:
        minimum, maximum = self._session.execute(
            select(func.min(MatchORM.scheduled_at), func.max(MatchORM.scheduled_at))
        ).one()
        if minimum is None or maximum is None:
            return ()
        first = match_export_season_for_datetime(from_database_utc(minimum, field_name="matches.scheduled_at minimum"))
        last = match_export_season_for_datetime(from_database_utc(maximum, field_name="matches.scheduled_at maximum"))
        candidates: list[MatchExportSeason] = []
        first_year = int(first.key[:4])
        last_year = int(last.key[:4])
        for year in range(last_year, first_year - 1, -1):
            for split in (2, 1):
                season = parse_match_export_season(f"{year:04d}-split-{split}")
                if season.starts_at > last.ends_at or season.ends_at <= first.starts_at:
                    continue
                candidates.append(season)

        normalized_query = query.casefold()
        choices: list[MatchExportSeasonChoice] = []
        for season in candidates:
            if (
                normalized_query
                and normalized_query not in season.key.casefold()
                and normalized_query not in season.name.casefold()
            ):
                continue
            exists = self._session.scalar(
                select(MatchORM.id)
                .where(
                    MatchORM.scheduled_at >= to_database_utc(season.starts_at),
                    MatchORM.scheduled_at < to_database_utc(season.ends_at),
                )
                .limit(1)
            )
            if exists is None:
                continue
            choices.append(MatchExportSeasonChoice(key=season.key, name=season.name))
            if len(choices) == limit:
                break
        return tuple(choices)

    def get_season_source(
        self,
        *,
        season: MatchExportSeason,
        source_cutoff: datetime,
    ) -> MatchSeasonExportSource | None:
        match_rows = tuple(
            self._session.execute(
                select(MatchORM, MatchConditionORM, StadiumCourseORM, StadiumORM)
                .join(StadiumCourseORM, StadiumCourseORM.id == MatchORM.stadium_course_id)
                .join(StadiumORM, StadiumORM.id == StadiumCourseORM.stadium_id)
                .outerjoin(MatchConditionORM, MatchConditionORM.match_id == MatchORM.id)
                .where(
                    MatchORM.scheduled_at >= to_database_utc(season.starts_at),
                    MatchORM.scheduled_at < to_database_utc(season.ends_at),
                )
                .order_by(MatchORM.scheduled_at, MatchORM.id)
            ).all()
        )
        if not match_rows:
            return None
        match_ids = tuple(match.id for match, _, _, _ in match_rows)

        entry_rows = tuple(
            self._session.execute(
                select(
                    MatchEntryORM,
                    GameAccountORM,
                    PersonaORM.display_name,
                    UmamusumeORM,
                    UmamusumeVariantORM,
                )
                .join(GameAccountORM, GameAccountORM.id == MatchEntryORM.game_account_id)
                .join(PersonaORM, PersonaORM.id == MatchEntryORM.owner_at_event_persona_id)
                .join(UmamusumeORM, UmamusumeORM.id == MatchEntryORM.umamusume_id)
                .outerjoin(UmamusumeVariantORM, UmamusumeVariantORM.id == MatchEntryORM.umamusume_variant_id)
                .where(MatchEntryORM.match_id.in_(match_ids))
                .order_by(MatchEntryORM.match_id, MatchEntryORM.entry_number, MatchEntryORM.id)
            ).all()
        )
        bet_rows = tuple(
            self._session.execute(
                select(BetORM, PersonaORM.display_name)
                .join(PersonaORM, PersonaORM.id == BetORM.persona_id)
                .where(BetORM.match_id.in_(match_ids))
                .order_by(BetORM.match_id, BetORM.created_at, BetORM.id)
            ).all()
        )
        entry_ids = tuple(entry.id for entry, _, _, _, _ in entry_rows)
        rating_rows = (
            tuple(
                self._session.execute(
                    select(RatingTransactionORM, RatingRuleVersionORM.version_number)
                    .join(
                        RatingRuleVersionORM,
                        RatingRuleVersionORM.id == RatingTransactionORM.rating_rule_version_id,
                    )
                    .where(RatingTransactionORM.match_entry_id.in_(entry_ids))
                    .order_by(
                        RatingTransactionORM.match_entry_id,
                        RatingTransactionORM.created_at,
                        RatingTransactionORM.id,
                    )
                ).all()
            )
            if entry_ids
            else ()
        )

        evidence_by_bet, settled_match_ids, cancelled_match_ids, rolled_back_match_ids = self._load_bet_evidence(
            match_ids=match_ids,
            bet_rows=bet_rows,
        )
        entries_by_match: dict[int, list[MatchExportEntry]] = defaultdict(list)
        entry_match: dict[int, int] = {}
        for entry, account, owner_name, character, variant in entry_rows:
            entry_match[entry.id] = entry.match_id
            entries_by_match[entry.match_id].append(
                MatchExportEntry(
                    id=entry.id,
                    entry_number=entry.entry_number,
                    game_account_id=entry.game_account_id,
                    game_account_name=account.nickname,
                    game_region=GameRegion(account.game_region),
                    owner_at_event_display_name=owner_name,
                    affiliation_at_event=entry.affiliation_at_event,
                    character_name=_display_name(character.name_ko, character.name_jp),
                    variant_name=(_display_name(variant.name_ko, variant.name_jp) if variant is not None else None),
                    running_style=entry.running_style,
                    training_grade=entry.training_grade,
                    rank=entry.rank,
                    rating_disposition=(
                        MatchRatingDisposition(entry.rating_disposition)
                        if entry.rating_disposition is not None
                        else None
                    ),
                    popularity_rank=entry.popularity_rank,
                    margin=entry.margin,
                )
            )

        bets_by_match: dict[int, list[MatchExportBet]] = defaultdict(list)
        for bet, display_name in bet_rows:
            evidence = evidence_by_bet.get(bet.id, _BetEvidence())
            bets_by_match[bet.match_id].append(
                MatchExportBet(
                    id=bet.id,
                    persona_display_name=display_name,
                    bet_type=BetType(bet.type),
                    selection_entry_ids=tuple(bet.selections),
                    amount=bet.amount,
                    status=BetStatus(bet.status),
                    created_at=from_database_utc(bet.created_at, field_name="bets.created_at"),
                    updated_at=from_database_utc(bet.updated_at, field_name="bets.updated_at"),
                    applied_odds=evidence.applied_odds,
                    payout_amount=evidence.payout_amount,
                    payout_group_bet_ids=evidence.payout_group_bet_ids,
                    payout_reversal_amount=evidence.payout_reversal_amount,
                    refund_amount=evidence.refund_amount,
                    refund_group_bet_ids=evidence.refund_group_bet_ids,
                    refund_reason=evidence.refund_reason,
                    refunded_at=evidence.refunded_at,
                )
            )

        ratings_by_match: dict[int, list[MatchExportRatingTransaction]] = defaultdict(list)
        for rating, version_number in rating_rows:
            match_id = entry_match.get(rating.match_entry_id)
            if match_id is None:
                raise ValueError("Rating transaction references an unavailable Match Entry.")
            ratings_by_match[match_id].append(
                MatchExportRatingTransaction(
                    id=rating.id,
                    match_entry_id=rating.match_entry_id,
                    rating_rule_version=version_number,
                    rating_before=rating.rating_before,
                    amount=rating.amount,
                    rating_after=rating.rating_after,
                    created_at=from_database_utc(
                        rating.created_at,
                        field_name="rating_transactions.created_at",
                    ),
                )
            )

        matches = tuple(
            MatchExportMatch(
                id=match.id,
                name=match.name,
                description=match.description,
                source_kind=MatchSourceKind(match.source_kind),
                grade=MatchGrade(match.grade),
                scheduled_at=from_database_utc(match.scheduled_at, field_name="matches.scheduled_at"),
                status=MatchStatus(match.status),
                terminal_reason=match.terminal_reason,
                finish_time_ms=match.finish_time_ms,
                course=MatchExportCourse(
                    stadium_name=_display_name(stadium.name_ko, stadium.name_jp),
                    course_id=course.id,
                    surface=MatchSurface(course.surface),
                    distance=course.distance,
                    direction=MatchDirection(course.direction),
                    layout=StadiumCourseLayout(course.layout),
                ),
                condition=(
                    MatchExportCondition(
                        season=MatchSeason(condition.season),
                        weather=MatchWeather(condition.weather),
                        time_of_day=MatchTimeOfDay(condition.time_of_day),
                        track_condition=MatchTrackCondition(condition.track_condition),
                    )
                    if condition is not None
                    else None
                ),
                settlement_evidence_present=match.id in settled_match_ids,
                cancellation_evidence_present=match.id in cancelled_match_ids,
                rollback_evidence_present=match.id in rolled_back_match_ids,
                entries=tuple(entries_by_match[match.id]),
                bets=tuple(bets_by_match[match.id]),
                ratings=tuple(ratings_by_match[match.id]),
            )
            for match, condition, course, stadium in match_rows
        )
        return MatchSeasonExportSource(
            season=season,
            source_cutoff=source_cutoff,
            matches=matches,
        )

    def _load_bet_evidence(
        self,
        *,
        match_ids: tuple[int, ...],
        bet_rows: tuple[object, ...],
    ) -> tuple[dict[int, _BetEvidence], set[int], set[int], set[int]]:
        evidence: dict[int, _BetEvidence] = {
            bet.id: _BetEvidence()
            for bet, _ in bet_rows  # type: ignore[misc]
        }
        bets_by_match: dict[int, list[object]] = defaultdict(list)
        for bet, _ in bet_rows:  # type: ignore[misc]
            bets_by_match[bet.match_id].append(bet)

        match_operations = tuple(
            self._session.execute(
                select(MatchOperationORM.match_id, MatchOperationORM.type, MatchOperationORM.after_data)
                .where(
                    MatchOperationORM.match_id.in_(match_ids),
                    MatchOperationORM.type.in_(
                        (
                            MatchSettlementAuditType.SETTLED.value,
                            MatchCancellationAuditType.CANCELLED.value,
                            MatchSettlementRollbackAuditType.ROLLED_BACK.value,
                        )
                    ),
                )
                .order_by(MatchOperationORM.match_id, MatchOperationORM.operation_id)
            ).all()
        )
        settlements: dict[int, SettledMatch] = {}
        cancellations: set[int] = set()
        rollbacks: dict[int, RolledBackMatchSettlement] = {}
        for match_id, operation_type, payload in match_operations:
            if not isinstance(payload, dict):
                raise ValueError("Match economic audit payload is missing.")
            if operation_type == MatchSettlementAuditType.SETTLED.value:
                if match_id in settlements:
                    raise ValueError("Multiple settlement audits reference one Match.")
                settled = SettledMatch.from_audit_payload(payload)
                settlements[match_id] = settled
                self._apply_settlement_evidence(
                    settled=settled,
                    bets=bets_by_match[match_id],
                    evidence=evidence,
                )
            elif operation_type == MatchCancellationAuditType.CANCELLED.value:
                if match_id in cancellations:
                    raise ValueError("Multiple cancellation audits reference one Match.")
                cancelled = CancelledMatch.from_audit_payload(payload)
                cancellations.add(match_id)
                for refund in cancelled.refunds:
                    self._set_refund(
                        evidence=evidence,
                        bet_ids=refund.bet_ids,
                        amount=refund.amount,
                        reason=cancelled.reason,
                        refunded_at=cancelled.cancelled_at,
                    )
            else:
                if match_id in rollbacks:
                    raise ValueError("Multiple settlement rollback audits reference one Match.")
                rollback = RolledBackMatchSettlement.from_audit_payload(payload)
                rollbacks[match_id] = rollback
                for compensation in rollback.point_compensations:
                    if compensation.action == MATCH_BET_REFUND_POINT_ACTION:
                        self._set_refund(
                            evidence=evidence,
                            bet_ids=compensation.refunded_bet_ids,
                            amount=compensation.amount,
                            reason=rollback.reason,
                            refunded_at=rollback.rolled_back_at,
                        )

        for match_id, rollback in rollbacks.items():
            settled = settlements.get(match_id)
            if settled is None:
                raise ValueError("Settlement rollback export evidence has no original settlement audit.")
            payout_groups = {payout.point_transaction_id: payout.bet_ids for payout in settled.payouts}
            for compensation in rollback.point_compensations:
                if compensation.action != MATCH_BET_PAYOUT_REVERSAL_POINT_ACTION:
                    continue
                group = payout_groups.get(compensation.original_point_transaction_id)  # type: ignore[arg-type]
                if group is None:
                    raise ValueError("Payout reversal cannot be mapped to stored settlement evidence.")
                for bet_id in group:
                    target = evidence.get(bet_id)
                    if target is None or target.payout_reversal_amount is not None:
                        raise ValueError("Payout reversal references an unavailable or duplicate Bet group.")
                    target.payout_reversal_amount = -compensation.amount

        replacement_rows = tuple(
            self._session.execute(
                select(BetOperationORM.after_data)
                .where(
                    BetOperationORM.match_id.in_(match_ids),
                    BetOperationORM.type == BetReplacementAuditType.REPLACED.value,
                )
                .order_by(BetOperationORM.operation_id)
            ).scalars()
        )
        for payload in replacement_rows:
            if not isinstance(payload, dict):
                raise ValueError("Bet replacement audit payload is missing.")
            replaced = ReplacedMatchBet.from_audit_payload(payload)
            self._set_refund(
                evidence=evidence,
                bet_ids=(replaced.old_bet.id,),
                amount=replaced.old_bet.amount,
                reason="bet_replaced",
                refunded_at=replaced.replaced_at,
            )
        return evidence, set(settlements), cancellations, set(rollbacks)

    @staticmethod
    def _apply_settlement_evidence(
        *,
        settled: SettledMatch,
        bets: list[object],
        evidence: dict[int, _BetEvidence],
    ) -> None:
        odds_by_market = {
            (market.bet_type, market.selection_entry_ids): market.confirmed_odds for market in settled.applied_odds
        }
        active_bet_ids = set(settled.active_bet_ids)
        for bet in bets:
            target = evidence[bet.id]  # type: ignore[attr-defined]
            if bet.id in active_bet_ids:  # type: ignore[attr-defined]
                target.applied_odds = odds_by_market.get(
                    (BetType(bet.type), tuple(bet.selections))  # type: ignore[attr-defined]
                )
        for payout in settled.payouts:
            for bet_id in payout.bet_ids:
                target = evidence.get(bet_id)
                if target is None or target.payout_amount is not None:
                    raise ValueError("Settlement payout references an unavailable or duplicate Bet.")
                target.payout_amount = payout.amount
                target.payout_group_bet_ids = payout.bet_ids

    @staticmethod
    def _set_refund(
        *,
        evidence: dict[int, _BetEvidence],
        bet_ids: tuple[int, ...],
        amount: int,
        reason: str | None,
        refunded_at: datetime,
    ) -> None:
        group = tuple(sorted(bet_ids))
        for bet_id in group:
            target = evidence.get(bet_id)
            if target is None or target.refund_amount is not None:
                raise ValueError("Refund audit references an unavailable or duplicate Bet.")
            target.refund_amount = amount
            target.refund_group_bet_ids = group
            target.refund_reason = reason
            target.refunded_at = refunded_at


class SqlAlchemyMatchSeasonExportUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW exposing complete Circle Match Season snapshots."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._exports: SqlAlchemyMatchSeasonExportRepository | None = None

    @property
    def match_season_exports(self) -> SqlAlchemyMatchSeasonExportRepository:
        return self._require_active_repository(self._exports)

    def _activate_repositories(self) -> None:
        self._exports = SqlAlchemyMatchSeasonExportRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._exports = None


class SqlAlchemyMatchSeasonExportUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchSeasonExportUnitOfWork]
):
    """Create one fresh Circle Match Season export query UoW."""

    unit_of_work_type = SqlAlchemyMatchSeasonExportUnitOfWork
