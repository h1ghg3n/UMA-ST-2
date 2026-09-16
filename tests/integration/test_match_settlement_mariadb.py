"""MariaDB atomic native V2 Match settlement evidence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Engine

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.match import (
    MATCH_BET_PAYOUT_POINT_ACTION,
    MATCH_BET_PAYOUT_REVERSAL_POINT_ACTION,
    MATCH_BET_REFUND_POINT_ACTION,
    MATCH_PLACEMENT_REWARD_POINT_ACTION,
    MATCH_PLACEMENT_REWARD_REVERSAL_POINT_ACTION,
    MatchResultCandidate,
    MatchResultCandidateEntry,
    MatchSettlementCommands,
    MatchSettlementQueries,
    MatchSettlementRatingSelectionError,
    MatchSettlementRollbackCommands,
    MatchSettlementRollbackEvidenceExpiredError,
    MatchSettlementRollbackQueries,
    MatchSettlementStaleError,
    RollbackMatchSettlement,
    SettleMatch,
)
from uma_st2.application.publication import (
    MATCH_RESULT_CONFIRMED_EVENT_TYPE,
    MATCH_SETTLEMENT_VOIDED_EVENT_TYPE,
)
from uma_st2.domain.match import MatchRatingDisposition
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyMatchSettlementQueryUnitOfWorkFactory,
    SqlAlchemyMatchSettlementRepository,
    SqlAlchemyMatchSettlementRollbackQueryUnitOfWorkFactory,
    SqlAlchemyMatchSettlementRollbackRepository,
    SqlAlchemyMatchSettlementRollbackUnitOfWork,
    SqlAlchemyMatchSettlementRollbackUnitOfWorkFactory,
    SqlAlchemyMatchSettlementUnitOfWork,
    SqlAlchemyMatchSettlementUnitOfWorkFactory,
)
from uma_st2.infrastructure.database.orm import (
    BetORM,
    CirclePointORM,
    DiscordPublicationORM,
    GameAccountORM,
    MatchConditionORM,
    MatchEntryORM,
    MatchOperationORM,
    MatchORM,
    MatchResultSubmissionORM,
    OperationORM,
    PersonaORM,
    PointTransactionORM,
    RatingORM,
    RatingRuleORM,
    RatingRuleVersionORM,
    RatingTransactionORM,
    StadiumCourseORM,
    StadiumORM,
    UmamusumeORM,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 28, 13, 0, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class SeededSettlement:
    match_id: int
    guild_id: str
    persona_ids: tuple[str, ...]
    account_ids: tuple[int, ...]
    umamusume_ids: tuple[int, ...]
    entry_ids: tuple[int, ...]
    bet_ids: tuple[int, ...]
    result_submission_id: int
    rating_rule_version_id: int
    stadium_id: int
    course_id: int


def _seed_settlement(engine: Engine, *, suffix: str) -> SeededSettlement:
    stored_now = (NOW - timedelta(days=1)).replace(tzinfo=None)
    external_base = int(suffix[:12], 16)
    guild_id = str(int(suffix[:15], 16) + 1)
    persona_ids = tuple(str(uuid4()) for _ in range(3))
    with engine.begin() as connection:
        connection.execute(
            PersonaORM.__table__.insert(),
            [
                {
                    "id": persona_id,
                    "display_name": f"Settlement Persona {index} {suffix}",
                    "status": "normal",
                    "created_at": stored_now,
                    "updated_at": stored_now,
                }
                for index, persona_id in enumerate(persona_ids, start=1)
            ],
        )
        connection.execute(
            CirclePointORM.__table__.insert(),
            [
                {"persona_id": persona_id, "balance": index * 100, "updated_at": stored_now}
                for index, persona_id in enumerate(persona_ids, start=1)
            ],
        )
        account_ids = tuple(
            connection.execute(
                GameAccountORM.__table__.insert().values(
                    persona_id=persona_id,
                    game_region="KR",
                    uma_pid=f"{index}{suffix[:15]}",
                    nickname=f"Settlement Account {index} {suffix}",
                    affiliation="A조",
                    created_at=stored_now,
                    updated_at=stored_now,
                )
            ).inserted_primary_key[0]
            for index, persona_id in enumerate(persona_ids, start=1)
        )
        umamusume_ids = tuple(
            connection.execute(
                UmamusumeORM.__table__.insert().values(
                    external_id=external_base + index,
                    name_jp=f"Horse JP {index} {suffix}",
                    name_ko=f"말 {index} {suffix}",
                    created_at=stored_now,
                    updated_at=stored_now,
                )
            ).inserted_primary_key[0]
            for index in range(1, 4)
        )
        stadium_id = connection.execute(
            StadiumORM.__table__.insert().values(
                external_id=external_base + 100,
                name_jp=f"Tokyo {suffix}",
                name_ko=f"도쿄 {suffix}",
                created_at=stored_now,
                updated_at=stored_now,
            )
        ).inserted_primary_key[0]
        course_id = connection.execute(
            StadiumCourseORM.__table__.insert().values(
                stadium_id=stadium_id,
                external_id=external_base + 101,
                surface="turf",
                distance=2400,
                direction="left",
                layout="standard",
                created_at=stored_now,
                updated_at=stored_now,
            )
        ).inserted_primary_key[0]
        match_id = connection.execute(
            MatchORM.__table__.insert().values(
                name=f"Settlement Match {suffix}",
                description="공식 룸매치",
                source_kind="native_v2",
                grade="G1",
                stadium_course_id=course_id,
                scheduled_at=SCHEDULED_AT.replace(tzinfo=None),
                status="result_confirmed",
                terminal_reason=None,
                finish_time_ms=123400,
                created_at=stored_now,
                updated_at=stored_now,
            )
        ).inserted_primary_key[0]
        connection.execute(
            MatchConditionORM.__table__.insert().values(
                match_id=match_id,
                season="autumn",
                weather="sunny",
                time_of_day="night",
                track_condition="firm",
                created_at=stored_now,
                updated_at=stored_now,
            )
        )
        entry_ids = tuple(
            connection.execute(
                MatchEntryORM.__table__.insert().values(
                    match_id=match_id,
                    game_account_id=account_id,
                    owner_at_event_persona_id=persona_id,
                    affiliation_at_event="A조",
                    umamusume_id=umamusume_id,
                    umamusume_variant_id=None,
                    entry_number=index,
                    running_style=None,
                    training_grade=None,
                    rank=index,
                    popularity_rank=index,
                    margin=None if index == 1 else "1/2",
                    created_at=stored_now,
                    updated_at=stored_now,
                )
            ).inserted_primary_key[0]
            for index, (account_id, persona_id, umamusume_id) in enumerate(
                zip(account_ids, persona_ids, umamusume_ids, strict=True),
                start=1,
            )
        )
        candidate = MatchResultCandidate(
            entries=tuple(
                MatchResultCandidateEntry(
                    entry_id=entry_id,
                    entry_number=index,
                    rank=index,
                    popularity_rank=index,
                    margin=None if index == 1 else "1/2",
                )
                for index, entry_id in enumerate(entry_ids, start=1)
            ),
            finish_time_ms=123400,
        )
        result_submission_id = connection.execute(
            MatchResultSubmissionORM.__table__.insert().values(
                match_id=match_id,
                revision_number=1,
                source_kind="manual",
                status="confirmed",
                pending_marker=None,
                confirmed_marker=True,
                candidate_json=candidate.to_payload(),
                submitted_operation_id=None,
                rejected_operation_id=None,
                rejected_at=None,
                rejection_reason=None,
                confirmed_operation_id=None,
                confirmed_at=stored_now,
                created_at=stored_now,
                updated_at=stored_now,
            )
        ).inserted_primary_key[0]
        connection.execute(
            RatingORM.__table__.insert(),
            [
                {"game_account_id": account_id, "rating": Decimal(100), "updated_at": stored_now}
                for account_id in account_ids[:2]
            ],
        )
        latest_version = connection.scalar(select(func.max(RatingRuleVersionORM.version_number))) or 0
        rating_rule_version_id = connection.execute(
            RatingRuleVersionORM.__table__.insert().values(
                version_number=latest_version + 1,
                source_identifier=f"settlement-{suffix}",
                source_checksum="a" * 64,
                source_sheet_name="G1",
                source_range="A1:D5",
                rule_set_checksum="b" * 64,
                rule_count=5,
                created_at=stored_now,
            )
        ).inserted_primary_key[0]
        connection.execute(
            RatingRuleORM.__table__.insert(),
            [
                {
                    "rating_rule_version_id": rating_rule_version_id,
                    "grade": "G1",
                    "participant_count": 2,
                    "converted_rank": rank,
                    "base_delta": Decimal(delta),
                    "created_at": stored_now,
                }
                for rank, delta in enumerate(("5", "-5"), start=1)
            ]
            + [
                {
                    "rating_rule_version_id": rating_rule_version_id,
                    "grade": "G1",
                    "participant_count": 3,
                    "converted_rank": rank,
                    "base_delta": Decimal(delta),
                    "created_at": stored_now,
                }
                for rank, delta in enumerate(("10", "0", "-5"), start=1)
            ],
        )
        bet_specs = (
            (persona_ids[0], "win", [entry_ids[0]], 10),
            (persona_ids[1], "quinella", [entry_ids[0], entry_ids[1]], 20),
            (persona_ids[2], "trio", list(entry_ids), 10),
            (persona_ids[2], "win", [entry_ids[1]], 10),
        )
        bet_ids = tuple(
            connection.execute(
                BetORM.__table__.insert().values(
                    match_id=match_id,
                    persona_id=persona_id,
                    type=bet_type,
                    selections=selections,
                    selection_fingerprint=f"{suffix}-{index}",
                    amount=amount,
                    status="active",
                    active_marker=True,
                    created_at=stored_now,
                    updated_at=stored_now,
                )
            ).inserted_primary_key[0]
            for index, (persona_id, bet_type, selections, amount) in enumerate(bet_specs, start=1)
        )
    return SeededSettlement(
        match_id=match_id,
        guild_id=guild_id,
        persona_ids=persona_ids,
        account_ids=account_ids,
        umamusume_ids=umamusume_ids,
        entry_ids=entry_ids,
        bet_ids=bet_ids,
        result_submission_id=result_submission_id,
        rating_rule_version_id=rating_rule_version_id,
        stadium_id=stadium_id,
        course_id=course_id,
    )


def _services(engine: Engine) -> tuple[MatchSettlementCommands, MatchSettlementQueries]:
    runtime = DatabaseRuntime.from_engine(engine)
    return (
        MatchSettlementCommands(
            CommandRunner(SqlAlchemyMatchSettlementUnitOfWorkFactory(runtime.session_factory)),
            clock=lambda: NOW,
        ),
        MatchSettlementQueries(QueryRunner(SqlAlchemyMatchSettlementQueryUnitOfWorkFactory(runtime.session_factory))),
    )


def _request(
    seeded: SeededSettlement,
    *,
    fingerprint: str,
    key: str,
    excluded_rating_entry_ids: tuple[int, ...] = (),
) -> SettleMatch:
    return SettleMatch(
        match_id=seeded.match_id,
        expected_settlement_fingerprint=fingerprint,
        excluded_rating_entry_ids=excluded_rating_entry_ids,
        idempotency_key=key,
        actor_discord_user_id="operator-1",
        guild_id=seeded.guild_id,
        reason="공식 정산 확정",
        correlation_id=key,
    )


def _rollback_services(
    engine: Engine,
) -> tuple[MatchSettlementRollbackCommands, MatchSettlementRollbackQueries]:
    runtime = DatabaseRuntime.from_engine(engine)
    return (
        MatchSettlementRollbackCommands(
            CommandRunner(SqlAlchemyMatchSettlementRollbackUnitOfWorkFactory(runtime.session_factory)),
            clock=lambda: NOW + timedelta(hours=1),
        ),
        MatchSettlementRollbackQueries(
            QueryRunner(SqlAlchemyMatchSettlementRollbackQueryUnitOfWorkFactory(runtime.session_factory))
        ),
    )


def _settle_for_rollback(
    engine: Engine,
    seeded: SeededSettlement,
    *,
    suffix: str,
):  # type: ignore[no-untyped-def]
    commands, queries = _services(engine)
    preview = queries.get_preview(match_id=seeded.match_id)
    return commands.settle_match(
        _request(
            seeded,
            fingerprint=preview.settlement_fingerprint,
            key=f"rollback-source-settlement-{suffix}",
        )
    )


def _rollback_request(
    seeded: SeededSettlement,
    *,
    fingerprint: str,
    key: str,
) -> RollbackMatchSettlement:
    return RollbackMatchSettlement(
        match_id=seeded.match_id,
        expected_rollback_fingerprint=fingerprint,
        idempotency_key=key,
        actor_discord_user_id="operator-1",
        guild_id=seeded.guild_id,
        reason="공식 결과 오류",
        correlation_id=key,
    )


def _cleanup(engine: Engine, seeded: SeededSettlement) -> None:
    with engine.begin() as connection:
        connection.execute(
            delete(DiscordPublicationORM).where(
                DiscordPublicationORM.source_kind == "match",
                DiscordPublicationORM.source_id == seeded.match_id,
            )
        )
        operation_ids = tuple(
            connection.scalars(
                select(MatchOperationORM.operation_id).where(MatchOperationORM.match_id == seeded.match_id)
            )
        )
        if operation_ids:
            connection.execute(delete(RatingTransactionORM).where(RatingTransactionORM.operation_id.in_(operation_ids)))
            connection.execute(delete(PointTransactionORM).where(PointTransactionORM.operation_id.in_(operation_ids)))
            connection.execute(delete(MatchOperationORM).where(MatchOperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(delete(BetORM).where(BetORM.match_id == seeded.match_id))
        connection.execute(delete(MatchResultSubmissionORM).where(MatchResultSubmissionORM.match_id == seeded.match_id))
        connection.execute(delete(RatingORM).where(RatingORM.game_account_id.in_(seeded.account_ids)))
        connection.execute(
            delete(RatingRuleORM).where(RatingRuleORM.rating_rule_version_id == seeded.rating_rule_version_id)
        )
        connection.execute(delete(RatingRuleVersionORM).where(RatingRuleVersionORM.id == seeded.rating_rule_version_id))
        connection.execute(delete(MatchEntryORM).where(MatchEntryORM.match_id == seeded.match_id))
        connection.execute(delete(MatchConditionORM).where(MatchConditionORM.match_id == seeded.match_id))
        connection.execute(delete(MatchORM).where(MatchORM.id == seeded.match_id))
        connection.execute(delete(StadiumCourseORM).where(StadiumCourseORM.id == seeded.course_id))
        connection.execute(delete(StadiumORM).where(StadiumORM.id == seeded.stadium_id))
        connection.execute(delete(CirclePointORM).where(CirclePointORM.persona_id.in_(seeded.persona_ids)))
        connection.execute(delete(GameAccountORM).where(GameAccountORM.id.in_(seeded.account_ids)))
        connection.execute(delete(UmamusumeORM).where(UmamusumeORM.id.in_(seeded.umamusume_ids)))
        connection.execute(delete(PersonaORM).where(PersonaORM.id.in_(seeded.persona_ids)))


def test_settlement_commits_one_complete_evidence_bundle_and_exact_retry(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_settlement(migrated_engine, suffix=suffix)
    commands, queries = _services(migrated_engine)
    try:
        choices = queries.search_targets(search=suffix, limit=10)
        assert tuple(choice.match_id for choice in choices) == (seeded.match_id,)
        preview = queries.get_preview(match_id=seeded.match_id)
        request = _request(
            seeded,
            fingerprint=preview.settlement_fingerprint,
            key=f"match-settlement-{suffix}",
        )

        settled = commands.settle_match(request)
        assert commands.settle_match(request) == settled
        assert queries.search_targets(search=suffix, limit=10) == ()

        with migrated_engine.connect() as connection:
            match_status = connection.scalar(select(MatchORM.status).where(MatchORM.id == seeded.match_id))
            bets = tuple(
                connection.execute(
                    select(BetORM.id, BetORM.status, BetORM.active_marker)
                    .where(BetORM.match_id == seeded.match_id)
                    .order_by(BetORM.id)
                )
            )
            balances = {
                row.persona_id: row.balance
                for row in connection.execute(
                    select(CirclePointORM.persona_id, CirclePointORM.balance).where(
                        CirclePointORM.persona_id.in_(seeded.persona_ids)
                    )
                )
            }
            rating_values = tuple(
                connection.scalars(
                    select(RatingORM.rating)
                    .where(RatingORM.game_account_id.in_(seeded.account_ids))
                    .order_by(RatingORM.game_account_id)
                )
            )
            rating_transactions = tuple(
                connection.execute(
                    select(
                        RatingTransactionORM.id,
                        RatingTransactionORM.rating_rule_version_id,
                        RatingTransactionORM.match_entry_id,
                        RatingTransactionORM.amount,
                    )
                    .join(OperationORM, OperationORM.id == RatingTransactionORM.operation_id)
                    .where(OperationORM.idempotency_key == request.idempotency_key)
                    .order_by(RatingTransactionORM.match_entry_id)
                )
            )
            point_transactions = tuple(
                connection.execute(
                    select(PointTransactionORM.id, PointTransactionORM.action, PointTransactionORM.amount)
                    .join(OperationORM, OperationORM.id == PointTransactionORM.operation_id)
                    .where(OperationORM.idempotency_key == request.idempotency_key)
                    .order_by(PointTransactionORM.id)
                )
            )
            audit = connection.execute(
                select(MatchOperationORM.type, MatchOperationORM.before_data, MatchOperationORM.after_data)
                .join(OperationORM, OperationORM.id == MatchOperationORM.operation_id)
                .where(OperationORM.idempotency_key == request.idempotency_key)
            ).one()
            publications = tuple(
                connection.execute(
                    select(
                        DiscordPublicationORM.guild_id,
                        DiscordPublicationORM.event_type,
                        DiscordPublicationORM.source_id,
                        DiscordPublicationORM.status,
                        DiscordPublicationORM.target_channel_id,
                        DiscordPublicationORM.payload_json,
                    ).where(
                        DiscordPublicationORM.source_kind == "match",
                        DiscordPublicationORM.source_id == seeded.match_id,
                    )
                )
            )

        assert match_status == "settled"
        assert bets == tuple((bet_id, "settled", None) for bet_id in seeded.bet_ids)
        assert balances == {
            seeded.persona_ids[0]: 261,
            seeded.persona_ids[1]: 314,
            seeded.persona_ids[2]: 375,
        }
        assert rating_values == tuple(rating.rating_after for rating in settled.ratings)
        assert tuple(row.rating_rule_version_id for row in rating_transactions) == (seeded.rating_rule_version_id,) * 3
        assert tuple(row.match_entry_id for row in rating_transactions) == seeded.entry_ids
        assert tuple(row.amount for row in rating_transactions) == tuple(rating.amount for rating in settled.ratings)
        assert sorted(row.action for row in point_transactions) == sorted(
            (MATCH_BET_PAYOUT_POINT_ACTION,) * 3 + (MATCH_PLACEMENT_REWARD_POINT_ACTION,) * 3
        )
        assert audit.type == "match_settled"
        assert audit.before_data["settlement_fingerprint"] == preview.settlement_fingerprint
        assert audit.after_data["active_bet_ids"] == list(seeded.bet_ids)
        assert audit.after_data["rating_transaction_ids"] == [row.id for row in rating_transactions]
        assert sorted(
            audit.after_data["payout_point_transaction_ids"] + audit.after_data["reward_point_transaction_ids"]
        ) == sorted(row.id for row in point_transactions)
        assert len(publications) == 1
        publication = publications[0]
        assert publication.guild_id == seeded.guild_id
        assert publication.event_type == MATCH_RESULT_CONFIRMED_EVENT_TYPE
        assert publication.source_id == seeded.match_id
        assert publication.status == "awaiting_channel"
        assert publication.target_channel_id is None
        assert publication.payload_json["match"]["name"] == settled.match_name
        assert settled.payout_total == 30
        assert settled.reward_total == 320
    finally:
        _cleanup(migrated_engine, seeded)


def test_settlement_persists_reviewed_rating_selection_for_duplicate_game_account(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_settlement(migrated_engine, suffix=suffix)
    commands, queries = _services(migrated_engine)
    try:
        with migrated_engine.begin() as connection:
            connection.execute(
                update(MatchEntryORM)
                .where(MatchEntryORM.id == seeded.entry_ids[1])
                .values(
                    game_account_id=seeded.account_ids[0],
                    owner_at_event_persona_id=seeded.persona_ids[0],
                )
            )

        selection = queries.get_rating_selection(match_id=seeded.match_id)
        assert [entry.game_account_id for entry in selection.entries] == [
            seeded.account_ids[0],
            seeded.account_ids[0],
            seeded.account_ids[2],
        ]
        with pytest.raises(MatchSettlementRatingSelectionError, match="each GameAccount"):
            queries.get_preview(match_id=seeded.match_id)

        excluded = (seeded.entry_ids[1],)
        preview = queries.get_preview(
            match_id=seeded.match_id,
            excluded_rating_entry_ids=excluded,
        )
        request = _request(
            seeded,
            fingerprint=preview.settlement_fingerprint,
            key=f"match-settlement-rating-selection-{suffix}",
            excluded_rating_entry_ids=excluded,
        )
        settled = commands.settle_match(request)
        assert commands.settle_match(request) == settled

        with migrated_engine.connect() as connection:
            dispositions = tuple(
                connection.execute(
                    select(MatchEntryORM.id, MatchEntryORM.rating_disposition)
                    .where(MatchEntryORM.match_id == seeded.match_id)
                    .order_by(MatchEntryORM.entry_number)
                )
            )
            rating_transactions = tuple(
                connection.execute(
                    select(
                        RatingTransactionORM.id,
                        RatingTransactionORM.match_entry_id,
                        RatingTransactionORM.amount,
                    )
                    .join(OperationORM, OperationORM.id == RatingTransactionORM.operation_id)
                    .where(OperationORM.idempotency_key == request.idempotency_key)
                    .order_by(RatingTransactionORM.match_entry_id)
                )
            )
            rating_values = {
                row.game_account_id: row.rating
                for row in connection.execute(
                    select(RatingORM.game_account_id, RatingORM.rating).where(
                        RatingORM.game_account_id.in_(seeded.account_ids)
                    )
                )
            }
            publication = connection.scalar(
                select(DiscordPublicationORM.payload_json).where(
                    DiscordPublicationORM.source_kind == "match",
                    DiscordPublicationORM.source_id == seeded.match_id,
                    DiscordPublicationORM.event_type == MATCH_RESULT_CONFIRMED_EVENT_TYPE,
                )
            )

        assert dispositions == (
            (seeded.entry_ids[0], MatchRatingDisposition.RATED.value),
            (seeded.entry_ids[1], MatchRatingDisposition.EXCLUDED.value),
            (seeded.entry_ids[2], MatchRatingDisposition.RATED.value),
        )
        assert tuple(row.match_entry_id for row in rating_transactions) == (
            seeded.entry_ids[0],
            seeded.entry_ids[2],
        )
        rated = tuple(rating for rating in settled.ratings if rating.rating_disposition is MatchRatingDisposition.RATED)
        assert tuple(rating.rating_rank for rating in rated) == (1, 2)
        assert rating_values[seeded.account_ids[0]] == rated[0].rating_after
        assert rating_values[seeded.account_ids[1]] == Decimal("100.000000000000000000")
        assert rating_values[seeded.account_ids[2]] == rated[1].rating_after
        assert settled.excluded_rating_entry_ids == excluded
        assert settled.payout_total == 30
        duplicate_account_owner_reward = next(
            reward for reward in settled.rewards if reward.selected_match_entry_id == seeded.entry_ids[0]
        )
        assert duplicate_account_owner_reward.suppressed_match_entry_ids == (seeded.entry_ids[1],)
        assert publication is not None
        assert publication["schema_version"] == 2
        assert [row["rating_disposition"] for row in publication["results"]] == [
            "rated",
            "excluded",
            "rated",
        ]

        rollback_commands, rollback_queries = _rollback_services(migrated_engine)
        rollback_preview = rollback_queries.get_preview(match_id=seeded.match_id)
        rolled_back = rollback_commands.rollback_settlement(
            _rollback_request(
                seeded,
                fingerprint=rollback_preview.rollback_fingerprint,
                key=f"match-settlement-rating-selection-rollback-{suffix}",
            )
        )
        assert tuple(item.original_rating_transaction_id for item in rolled_back.rating_compensations) == tuple(
            row.id for row in rating_transactions
        )
        with migrated_engine.connect() as connection:
            retained_dispositions = tuple(
                connection.scalars(
                    select(MatchEntryORM.rating_disposition)
                    .where(MatchEntryORM.match_id == seeded.match_id)
                    .order_by(MatchEntryORM.entry_number)
                )
            )
        assert retained_dispositions == ("rated", "excluded", "rated")
    finally:
        _cleanup(migrated_engine, seeded)


def test_final_settlement_revalidates_changed_active_pool_with_zero_write(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_settlement(migrated_engine, suffix=suffix)
    commands, queries = _services(migrated_engine)
    try:
        preview = queries.get_preview(match_id=seeded.match_id)
        stored_now = NOW.replace(tzinfo=None)
        with migrated_engine.begin() as connection:
            connection.execute(
                BetORM.__table__.insert().values(
                    match_id=seeded.match_id,
                    persona_id=seeded.persona_ids[0],
                    type="win",
                    selections=[seeded.entry_ids[0]],
                    selection_fingerprint=f"stale-{suffix}",
                    amount=10,
                    status="active",
                    active_marker=True,
                    created_at=stored_now,
                    updated_at=stored_now,
                )
            )
        request = _request(
            seeded,
            fingerprint=preview.settlement_fingerprint,
            key=f"stale-match-settlement-{suffix}",
        )

        with pytest.raises(MatchSettlementStaleError):
            commands.settle_match(request)

        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(select(MatchORM.status).where(MatchORM.id == seeded.match_id)) == "result_confirmed"
            )
            assert (
                connection.scalar(
                    select(func.count(OperationORM.id)).where(OperationORM.idempotency_key == request.idempotency_key)
                )
                == 0
            )
            assert (
                connection.scalar(
                    select(func.count(BetORM.id)).where(
                        BetORM.match_id == seeded.match_id,
                        BetORM.status == "active",
                        BetORM.active_marker.is_(True),
                    )
                )
                == 5
            )
    finally:
        _cleanup(migrated_engine, seeded)


def test_concurrent_exact_retry_converges_to_one_settlement_bundle(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_settlement(migrated_engine, suffix=suffix)
    _, queries = _services(migrated_engine)
    preview = queries.get_preview(match_id=seeded.match_id)
    request = _request(
        seeded,
        fingerprint=preview.settlement_fingerprint,
        key=f"concurrent-match-settlement-{suffix}",
    )
    start = Barrier(2)

    def run() -> tuple[int, int, int]:
        commands, _ = _services(migrated_engine)
        start.wait()
        result = commands.settle_match(request)
        return result.payout_total, result.reward_total, len(result.rating_transaction_ids)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            receipts = tuple(future.result() for future in (executor.submit(run), executor.submit(run)))

        assert receipts == ((30, 320, 3), (30, 320, 3))
        with migrated_engine.connect() as connection:
            operation_count = connection.scalar(
                select(func.count(MatchOperationORM.operation_id)).where(
                    MatchOperationORM.match_id == seeded.match_id,
                    MatchOperationORM.type == "match_settled",
                )
            )
            point_count = connection.scalar(
                select(func.count(PointTransactionORM.id))
                .join(OperationORM, OperationORM.id == PointTransactionORM.operation_id)
                .where(OperationORM.idempotency_key == request.idempotency_key)
            )
            rating_count = connection.scalar(
                select(func.count(RatingTransactionORM.id))
                .join(OperationORM, OperationORM.id == RatingTransactionORM.operation_id)
                .where(OperationORM.idempotency_key == request.idempotency_key)
            )
            publication_count = connection.scalar(
                select(func.count(DiscordPublicationORM.id)).where(
                    DiscordPublicationORM.source_kind == "match",
                    DiscordPublicationORM.source_id == seeded.match_id,
                    DiscordPublicationORM.event_type == MATCH_RESULT_CONFIRMED_EVENT_TYPE,
                )
            )
        assert (operation_count, point_count, rating_count, publication_count) == (1, 6, 3, 1)
    finally:
        _cleanup(migrated_engine, seeded)


class FailingSettlementRepository(SqlAlchemyMatchSettlementRepository):
    def add_result_publication(self, **kwargs):  # type: ignore[no-untyped-def]
        super().add_result_publication(**kwargs)
        raise RuntimeError("injected failure after settlement and publication flush")


class FailingSettlementUnitOfWork(SqlAlchemyMatchSettlementUnitOfWork):
    def _activate_repositories(self) -> None:
        self._repository = FailingSettlementRepository(self.session)


class FailingSettlementFactory:
    def __init__(self, session_factory) -> None:  # type: ignore[no-untyped-def]
        self._session_factory = session_factory

    def __call__(self) -> FailingSettlementUnitOfWork:
        return FailingSettlementUnitOfWork(self._session_factory)


def test_exception_after_full_flush_rolls_back_every_settlement_write(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_settlement(migrated_engine, suffix=suffix)
    runtime = DatabaseRuntime.from_engine(migrated_engine)
    _, queries = _services(migrated_engine)
    preview = queries.get_preview(match_id=seeded.match_id)
    request = _request(
        seeded,
        fingerprint=preview.settlement_fingerprint,
        key=f"rollback-match-settlement-{suffix}",
    )
    commands = MatchSettlementCommands(
        CommandRunner(FailingSettlementFactory(runtime.session_factory)),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    try:
        with pytest.raises(RuntimeError, match="publication flush"):
            commands.settle_match(request)

        with migrated_engine.connect() as connection:
            match_status = connection.scalar(select(MatchORM.status).where(MatchORM.id == seeded.match_id))
            active_bet_count = connection.scalar(
                select(func.count(BetORM.id)).where(
                    BetORM.match_id == seeded.match_id,
                    BetORM.status == "active",
                    BetORM.active_marker.is_(True),
                )
            )
            balances = {
                row.persona_id: row.balance
                for row in connection.execute(
                    select(CirclePointORM.persona_id, CirclePointORM.balance).where(
                        CirclePointORM.persona_id.in_(seeded.persona_ids)
                    )
                )
            }
            ratings = tuple(
                connection.execute(
                    select(RatingORM.game_account_id, RatingORM.rating)
                    .where(RatingORM.game_account_id.in_(seeded.account_ids))
                    .order_by(RatingORM.game_account_id)
                )
            )
            operation_count = connection.scalar(
                select(func.count(OperationORM.id)).where(OperationORM.idempotency_key == request.idempotency_key)
            )
            publication_count = connection.scalar(
                select(func.count(DiscordPublicationORM.id)).where(
                    DiscordPublicationORM.source_kind == "match",
                    DiscordPublicationORM.source_id == seeded.match_id,
                )
            )
            rating_dispositions = tuple(
                connection.scalars(
                    select(MatchEntryORM.rating_disposition)
                    .where(MatchEntryORM.match_id == seeded.match_id)
                    .order_by(MatchEntryORM.entry_number)
                )
            )
        assert match_status == "result_confirmed"
        assert active_bet_count == 4
        assert balances == {
            seeded.persona_ids[0]: 100,
            seeded.persona_ids[1]: 200,
            seeded.persona_ids[2]: 300,
        }
        assert ratings == tuple(
            (account_id, Decimal("100.000000000000000000")) for account_id in seeded.account_ids[:2]
        )
        assert operation_count == 0
        assert publication_count == 0
        assert rating_dispositions == (None, None, None)
    finally:
        _cleanup(migrated_engine, seeded)


def test_terminal_rollback_compensates_complete_settlement_and_exact_retry(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_settlement(migrated_engine, suffix=suffix)
    settled = _settle_for_rollback(migrated_engine, seeded, suffix=suffix)
    commands, queries = _rollback_services(migrated_engine)
    try:
        choices = queries.search_targets(search=suffix, limit=10)
        assert tuple(choice.match_id for choice in choices) == (seeded.match_id,)
        preview = queries.get_preview(match_id=seeded.match_id)
        request = _rollback_request(
            seeded,
            fingerprint=preview.rollback_fingerprint,
            key=f"match-settlement-rollback-{suffix}",
        )

        rolled_back = commands.rollback_settlement(request)
        assert commands.rollback_settlement(request) == rolled_back
        assert queries.search_targets(search=suffix, limit=10) == ()

        with migrated_engine.connect() as connection:
            match_row = connection.execute(
                select(MatchORM.status, MatchORM.terminal_reason).where(MatchORM.id == seeded.match_id)
            ).one()
            bets = tuple(
                connection.execute(
                    select(BetORM.id, BetORM.status, BetORM.active_marker)
                    .where(BetORM.match_id == seeded.match_id)
                    .order_by(BetORM.id)
                )
            )
            balances = {
                row.persona_id: row.balance
                for row in connection.execute(
                    select(CirclePointORM.persona_id, CirclePointORM.balance).where(
                        CirclePointORM.persona_id.in_(seeded.persona_ids)
                    )
                )
            }
            ratings = tuple(
                connection.scalars(
                    select(RatingORM.rating)
                    .where(RatingORM.game_account_id.in_(seeded.account_ids))
                    .order_by(RatingORM.game_account_id)
                )
            )
            point_rows = tuple(
                connection.execute(
                    select(PointTransactionORM.id, PointTransactionORM.action, PointTransactionORM.amount)
                    .join(OperationORM, OperationORM.id == PointTransactionORM.operation_id)
                    .where(OperationORM.idempotency_key == request.idempotency_key)
                    .order_by(PointTransactionORM.id)
                )
            )
            rating_rows = tuple(
                connection.execute(
                    select(
                        RatingTransactionORM.id,
                        RatingTransactionORM.rating_before,
                        RatingTransactionORM.amount,
                        RatingTransactionORM.rating_after,
                    )
                    .join(OperationORM, OperationORM.id == RatingTransactionORM.operation_id)
                    .where(OperationORM.idempotency_key == request.idempotency_key)
                    .order_by(RatingTransactionORM.id)
                )
            )
            audit = connection.execute(
                select(MatchOperationORM.type, MatchOperationORM.before_data, MatchOperationORM.after_data)
                .join(OperationORM, OperationORM.id == MatchOperationORM.operation_id)
                .where(OperationORM.idempotency_key == request.idempotency_key)
            ).one()
            correction = connection.execute(
                select(
                    DiscordPublicationORM.event_key,
                    DiscordPublicationORM.status,
                    DiscordPublicationORM.target_channel_id,
                    DiscordPublicationORM.payload_json,
                ).where(
                    DiscordPublicationORM.source_kind == "match",
                    DiscordPublicationORM.source_id == seeded.match_id,
                    DiscordPublicationORM.event_type == MATCH_SETTLEMENT_VOIDED_EVENT_TYPE,
                )
            ).one()

        assert match_row == ("voided", "공식 결과 오류")
        assert bets == tuple((bet_id, "cancelled", None) for bet_id in seeded.bet_ids)
        assert balances == {
            seeded.persona_ids[0]: 110,
            seeded.persona_ids[1]: 220,
            seeded.persona_ids[2]: 320,
        }
        assert ratings == (
            Decimal("100.000000000000000000"),
            Decimal("100.000000000000000000"),
            Decimal("0.000000000000000000"),
        )
        assert sorted(row.action for row in point_rows) == sorted(
            (MATCH_BET_PAYOUT_REVERSAL_POINT_ACTION,) * 3
            + (MATCH_BET_REFUND_POINT_ACTION,) * 3
            + (MATCH_PLACEMENT_REWARD_REVERSAL_POINT_ACTION,) * 3
        )
        assert sum(row.amount for row in point_rows) == -300
        assert tuple(row.amount for row in rating_rows) == tuple(-item.amount for item in settled.ratings)
        assert tuple(row.rating_after for row in rating_rows) == tuple(item.rating_before for item in settled.ratings)
        assert audit.type == "match_settlement_rolled_back"
        assert audit.before_data["settlement_fingerprint"] == settled.settlement_fingerprint
        assert audit.before_data["active_bet_ids"] == list(seeded.bet_ids)
        assert audit.after_data["cancelled_bet_ids"] == list(seeded.bet_ids)
        assert audit.after_data["point_compensation_transaction_ids"] == [row.id for row in point_rows]
        assert audit.after_data["rating_compensation_transaction_ids"] == [row.id for row in rating_rows]
        assert correction.event_key == f"match:{seeded.match_id}:settlement-voided:v1"
        assert correction.status == "awaiting_channel"
        assert correction.target_channel_id is None
        assert correction.payload_json == {
            "schema_version": 1,
            "publication_type": "match_settlement_voided",
            "match": {
                "name": f"Settlement Match {suffix}",
                "grade": "G1",
                "scheduled_at": SCHEDULED_AT.isoformat(),
            },
            "rollback": {
                "completed_at": (NOW + timedelta(hours=1)).isoformat(),
                "reason": "공식 결과 오류",
                "prior_settlement_voided": True,
                "compensation_completed": True,
            },
        }
    finally:
        _cleanup(migrated_engine, seeded)


def test_later_rating_transaction_expires_rollback_with_zero_write(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_settlement(migrated_engine, suffix=suffix)
    settled = _settle_for_rollback(migrated_engine, seeded, suffix=suffix)
    _, queries = _rollback_services(migrated_engine)
    preview = queries.get_preview(match_id=seeded.match_id)
    request = _rollback_request(
        seeded,
        fingerprint=preview.rollback_fingerprint,
        key=f"later-rating-rollback-{suffix}",
    )
    later_operation_key = f"later-rating-{suffix}"
    try:
        stored_now = (NOW + timedelta(minutes=30)).replace(tzinfo=None)
        with migrated_engine.begin() as connection:
            operation_id = connection.execute(
                OperationORM.__table__.insert().values(
                    guild_id=seeded.guild_id,
                    correlation_id=later_operation_key,
                    actor_discord_user_id="operator-2",
                    idempotency_key=later_operation_key,
                    request_fingerprint="d" * 64,
                    reason="later Rating evidence",
                    created_at=stored_now,
                )
            ).inserted_primary_key[0]
            connection.execute(
                MatchOperationORM.__table__.insert().values(
                    operation_id=operation_id,
                    match_id=seeded.match_id,
                    type="test_later_rating",
                    before_data=None,
                    after_data=None,
                )
            )
            first_rating = settled.ratings[0]
            later_after = first_rating.rating_after + Decimal("1.000000000000000000")
            connection.execute(
                RatingTransactionORM.__table__.insert().values(
                    operation_id=operation_id,
                    rating_rule_version_id=seeded.rating_rule_version_id,
                    match_entry_id=first_rating.match_entry_id,
                    rating_before=first_rating.rating_after,
                    amount=Decimal("1.000000000000000000"),
                    rating_after=later_after,
                    created_at=stored_now,
                )
            )
            connection.execute(
                update(RatingORM)
                .where(RatingORM.game_account_id == first_rating.game_account_id)
                .values(rating=later_after, updated_at=stored_now)
            )

        commands, _ = _rollback_services(migrated_engine)
        with pytest.raises(MatchSettlementRollbackEvidenceExpiredError, match="no longer latest"):
            commands.rollback_settlement(request)

        with migrated_engine.connect() as connection:
            assert connection.scalar(select(MatchORM.status).where(MatchORM.id == seeded.match_id)) == "settled"
            assert (
                connection.scalar(
                    select(func.count(OperationORM.id)).where(OperationORM.idempotency_key == request.idempotency_key)
                )
                == 0
            )
            assert connection.scalar(
                select(func.count(BetORM.id)).where(
                    BetORM.match_id == seeded.match_id,
                    BetORM.status == "settled",
                )
            ) == len(seeded.bet_ids)
    finally:
        _cleanup(migrated_engine, seeded)


def test_concurrent_rollback_exact_retry_converges_to_one_compensation_bundle(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_settlement(migrated_engine, suffix=suffix)
    _settle_for_rollback(migrated_engine, seeded, suffix=suffix)
    _, queries = _rollback_services(migrated_engine)
    preview = queries.get_preview(match_id=seeded.match_id)
    request = _rollback_request(
        seeded,
        fingerprint=preview.rollback_fingerprint,
        key=f"concurrent-settlement-rollback-{suffix}",
    )
    start = Barrier(2)

    def run() -> tuple[int, int, int]:
        commands, _ = _rollback_services(migrated_engine)
        start.wait()
        result = commands.rollback_settlement(request)
        return (
            len(result.cancelled_bet_ids),
            len(result.point_compensations),
            len(result.rating_compensations),
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            receipts = tuple(future.result() for future in (executor.submit(run), executor.submit(run)))

        assert receipts == ((4, 9, 3), (4, 9, 3))
        with migrated_engine.connect() as connection:
            operation_count = connection.scalar(
                select(func.count(MatchOperationORM.operation_id)).where(
                    MatchOperationORM.match_id == seeded.match_id,
                    MatchOperationORM.type == "match_settlement_rolled_back",
                )
            )
            point_count = connection.scalar(
                select(func.count(PointTransactionORM.id))
                .join(OperationORM, OperationORM.id == PointTransactionORM.operation_id)
                .where(OperationORM.idempotency_key == request.idempotency_key)
            )
            rating_count = connection.scalar(
                select(func.count(RatingTransactionORM.id))
                .join(OperationORM, OperationORM.id == RatingTransactionORM.operation_id)
                .where(OperationORM.idempotency_key == request.idempotency_key)
            )
            publication_count = connection.scalar(
                select(func.count(DiscordPublicationORM.id)).where(
                    DiscordPublicationORM.source_kind == "match",
                    DiscordPublicationORM.source_id == seeded.match_id,
                    DiscordPublicationORM.event_type == MATCH_SETTLEMENT_VOIDED_EVENT_TYPE,
                )
            )
        assert (operation_count, point_count, rating_count, publication_count) == (1, 9, 3, 1)
    finally:
        _cleanup(migrated_engine, seeded)


class FailingRollbackRepository(SqlAlchemyMatchSettlementRollbackRepository):
    def add_rollback_publication(self, **kwargs):  # type: ignore[no-untyped-def]
        super().add_rollback_publication(**kwargs)
        raise RuntimeError("injected failure after rollback publication flush")


class FailingRollbackUnitOfWork(SqlAlchemyMatchSettlementRollbackUnitOfWork):
    def _activate_repositories(self) -> None:
        self._repository = FailingRollbackRepository(self.session)


class FailingRollbackFactory:
    def __init__(self, session_factory) -> None:  # type: ignore[no-untyped-def]
        self._session_factory = session_factory

    def __call__(self) -> FailingRollbackUnitOfWork:
        return FailingRollbackUnitOfWork(self._session_factory)


def test_exception_after_full_flush_rolls_back_every_compensation_write(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_settlement(migrated_engine, suffix=suffix)
    settled = _settle_for_rollback(migrated_engine, seeded, suffix=suffix)
    runtime = DatabaseRuntime.from_engine(migrated_engine)
    _, queries = _rollback_services(migrated_engine)
    preview = queries.get_preview(match_id=seeded.match_id)
    request = _rollback_request(
        seeded,
        fingerprint=preview.rollback_fingerprint,
        key=f"failing-settlement-rollback-{suffix}",
    )
    commands = MatchSettlementRollbackCommands(
        CommandRunner(FailingRollbackFactory(runtime.session_factory)),  # type: ignore[arg-type]
        clock=lambda: NOW + timedelta(hours=1),
    )
    try:
        with pytest.raises(RuntimeError, match="injected failure"):
            commands.rollback_settlement(request)

        with migrated_engine.connect() as connection:
            match_row = connection.execute(
                select(MatchORM.status, MatchORM.terminal_reason).where(MatchORM.id == seeded.match_id)
            ).one()
            settled_bet_count = connection.scalar(
                select(func.count(BetORM.id)).where(
                    BetORM.match_id == seeded.match_id,
                    BetORM.status == "settled",
                )
            )
            balances = {
                row.persona_id: row.balance
                for row in connection.execute(
                    select(CirclePointORM.persona_id, CirclePointORM.balance).where(
                        CirclePointORM.persona_id.in_(seeded.persona_ids)
                    )
                )
            }
            ratings = tuple(
                connection.scalars(
                    select(RatingORM.rating)
                    .where(RatingORM.game_account_id.in_(seeded.account_ids))
                    .order_by(RatingORM.game_account_id)
                )
            )
            rollback_operation_count = connection.scalar(
                select(func.count(OperationORM.id)).where(OperationORM.idempotency_key == request.idempotency_key)
            )
            rollback_publication_count = connection.scalar(
                select(func.count(DiscordPublicationORM.id)).where(
                    DiscordPublicationORM.source_kind == "match",
                    DiscordPublicationORM.source_id == seeded.match_id,
                    DiscordPublicationORM.event_type == MATCH_SETTLEMENT_VOIDED_EVENT_TYPE,
                )
            )

        assert match_row == ("settled", None)
        assert settled_bet_count == len(seeded.bet_ids)
        assert balances == {
            seeded.persona_ids[0]: 261,
            seeded.persona_ids[1]: 314,
            seeded.persona_ids[2]: 375,
        }
        assert ratings == tuple(item.rating_after for item in settled.ratings)
        assert rollback_operation_count == 0
        assert rollback_publication_count == 0
    finally:
        _cleanup(migrated_engine, seeded)
