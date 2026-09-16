"""MariaDB atomic transition, audit, publication, retry, and fail-closed opening evidence."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
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
    ChangeMatchOddsRefreshMode,
    MatchBettingOpenCommands,
    MatchBettingOpenIncompleteError,
    MatchBettingOpenInvalidSourceError,
    MatchBettingOpenStaleError,
    MatchConditionValues,
    MatchOddsPublicationCommands,
    MatchOddsPublicationInvalidSourceError,
    MatchOddsPublicationQueries,
    MatchSetupCommands,
    MatchSetupUnavailableError,
    MatchStaffBettingOpenQueries,
    MatchStaffSetupQueries,
    OpenMatchBetting,
    UpdateMatchSetup,
)
from uma_st2.application.publication import (
    MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    MATCH_BETTING_OPENED_EVENT_TYPE,
    MATCH_ODDS_REFRESH_EVENT_TYPE,
    MatchOddsRefreshMode,
)
from uma_st2.compose import compose_publication_delivery
from uma_st2.domain.match import (
    MatchGrade,
    MatchSeason,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
)
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyMatchBettingOpenUnitOfWorkFactory,
    SqlAlchemyMatchOddsPublicationQueryUnitOfWorkFactory,
    SqlAlchemyMatchOddsPublicationUnitOfWorkFactory,
    SqlAlchemyMatchSetupUnitOfWorkFactory,
    SqlAlchemyMatchStaffBettingOpenQueryUnitOfWorkFactory,
    SqlAlchemyMatchStaffSetupQueryUnitOfWorkFactory,
)
from uma_st2.infrastructure.database.orm import (
    BetORM,
    BotGuildSettingORM,
    DiscordPublicationORM,
    GameAccountORM,
    MatchConditionORM,
    MatchEntryORM,
    MatchOperationORM,
    MatchORM,
    OperationORM,
    PersonaORM,
    RatingRuleORM,
    RatingRuleVersionORM,
    SettingsOperationORM,
    StadiumCourseORM,
    StadiumORM,
    UmamusumeORM,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 28, 7, 0, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class SeededOpeningMatch:
    match_id: int
    guild_id: str
    channel_id: str
    stadium_id: int
    course_id: int
    persona_ids: tuple[str, ...]
    account_ids: tuple[int, ...]
    umamusume_ids: tuple[int, ...]
    entry_ids: tuple[int, ...]
    rating_rule_version_id: int | None


def _seed_opening_match(
    engine: Engine,
    *,
    suffix: str,
    entry_count: int = 3,
    grade: MatchGrade = MatchGrade.G1,
    rating_rule_version_complete: bool = True,
    covered_converted_ranks: tuple[int, ...] | None = None,
    seed_rating_rule_version: bool = True,
) -> SeededOpeningMatch:
    stored_now = (NOW - timedelta(days=1)).replace(tzinfo=None)
    external_base = int(suffix[:12], 16)
    guild_id = str(int(suffix[:15], 16) + 1)
    channel_id = str(int(suffix[15:30], 16) + 1)
    persona_ids = tuple(str(uuid4()) for _ in range(entry_count))
    with engine.begin() as connection:
        connection.execute(
            PersonaORM.__table__.insert(),
            [
                {
                    "id": persona_id,
                    "display_name": f"Persona {index} {suffix}",
                    "status": "normal",
                    "created_at": stored_now,
                    "updated_at": stored_now,
                }
                for index, persona_id in enumerate(persona_ids, start=1)
            ],
        )
        account_ids = tuple(
            connection.execute(
                GameAccountORM.__table__.insert().values(
                    persona_id=persona_id,
                    game_region="KR",
                    uma_pid=f"{index}{suffix[:15]}",
                    nickname=f"Account {index} {suffix}",
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
            for index in range(1, entry_count + 1)
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
                name=f"Opening Match {suffix}",
                description="공식 룸매치",
                source_kind="native_v2",
                grade=grade.value,
                stadium_course_id=course_id,
                scheduled_at=SCHEDULED_AT.replace(tzinfo=None),
                status="scheduled",
                terminal_reason=None,
                finish_time_ms=None,
                created_at=stored_now,
                updated_at=stored_now,
            )
        ).inserted_primary_key[0]
        connection.execute(
            MatchConditionORM.__table__.insert().values(
                match_id=match_id,
                season="autumn",
                weather="sunny",
                time_of_day="day",
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
                    rank=None,
                    popularity_rank=None,
                    margin=None,
                    created_at=stored_now,
                    updated_at=stored_now,
                )
            ).inserted_primary_key[0]
            for index, (account_id, persona_id, umamusume_id) in enumerate(
                zip(account_ids, persona_ids, umamusume_ids, strict=True),
                start=1,
            )
        )
        connection.execute(
            BotGuildSettingORM.__table__.insert().values(
                guild_id=guild_id,
                win5_announcement_channel_id=None,
                match_announcement_channel_id=channel_id,
                log_channel_id=None,
                operator_role_id=None,
                bot_manager_role_id=None,
                default_timezone="Asia/Seoul",
                win5_announcements_enabled=True,
                match_announcements_enabled=True,
                created_at=stored_now,
                updated_at=stored_now,
            )
        )
        rating_rule_version_id: int | None = None
        if seed_rating_rule_version:
            if covered_converted_ranks is None:
                if grade in (MatchGrade.G1, MatchGrade.G2, MatchGrade.G3):
                    rule_grade = grade
                    participant_count = entry_count
                    covered_converted_ranks = tuple(range(1, entry_count + 1))
                else:
                    rule_grade = MatchGrade.G1
                    participant_count = 2
                    covered_converted_ranks = (1, 2)
            else:
                rule_grade = grade
                participant_count = entry_count
            latest_version = connection.scalar(select(func.max(RatingRuleVersionORM.version_number))) or 0
            declared_rule_count = len(covered_converted_ranks) + (0 if rating_rule_version_complete else 1)
            rating_rule_version_id = connection.execute(
                RatingRuleVersionORM.__table__.insert().values(
                    version_number=latest_version + 1,
                    source_identifier=f"betting-open-{suffix}",
                    source_checksum="c" * 64,
                    source_sheet_name=rule_grade.value,
                    source_range=f"A1:D{max(len(covered_converted_ranks), 1)}",
                    rule_set_checksum="d" * 64,
                    rule_count=declared_rule_count,
                    created_at=stored_now,
                )
            ).inserted_primary_key[0]
            if covered_converted_ranks:
                connection.execute(
                    RatingRuleORM.__table__.insert(),
                    [
                        {
                            "rating_rule_version_id": rating_rule_version_id,
                            "grade": rule_grade.value,
                            "participant_count": participant_count,
                            "converted_rank": rank,
                            "base_delta": Decimal(entry_count - rank),
                            "created_at": stored_now,
                        }
                        for rank in covered_converted_ranks
                    ],
                )
    return SeededOpeningMatch(
        match_id=match_id,
        guild_id=guild_id,
        channel_id=channel_id,
        stadium_id=stadium_id,
        course_id=course_id,
        persona_ids=persona_ids,
        account_ids=account_ids,
        umamusume_ids=umamusume_ids,
        entry_ids=entry_ids,
        rating_rule_version_id=rating_rule_version_id,
    )


def _services(engine: Engine) -> tuple[MatchBettingOpenCommands, MatchStaffBettingOpenQueries]:
    runtime = DatabaseRuntime.from_engine(engine)
    return (
        MatchBettingOpenCommands(
            CommandRunner(SqlAlchemyMatchBettingOpenUnitOfWorkFactory(runtime.session_factory)),
            clock=lambda: NOW,
        ),
        MatchStaffBettingOpenQueries(
            QueryRunner(SqlAlchemyMatchStaffBettingOpenQueryUnitOfWorkFactory(runtime.session_factory))
        ),
    )


def _odds_services(
    engine: Engine,
    *,
    now: datetime,
) -> tuple[MatchOddsPublicationCommands, MatchOddsPublicationQueries]:
    runtime = DatabaseRuntime.from_engine(engine)
    return (
        MatchOddsPublicationCommands(
            CommandRunner(SqlAlchemyMatchOddsPublicationUnitOfWorkFactory(runtime.session_factory)),
            clock=lambda: now,
        ),
        MatchOddsPublicationQueries(
            QueryRunner(SqlAlchemyMatchOddsPublicationQueryUnitOfWorkFactory(runtime.session_factory))
        ),
    )


def _request(seeded: SeededOpeningMatch, *, fingerprint: str, key: str) -> OpenMatchBetting:
    return OpenMatchBetting(
        match_id=seeded.match_id,
        expected_state_fingerprint=fingerprint,
        idempotency_key=key,
        actor_discord_user_id="123456789",
        guild_id=seeded.guild_id,
        correlation_id=key,
    )


def _cleanup(engine: Engine, seeded: SeededOpeningMatch) -> None:
    with engine.begin() as connection:
        operation_ids = tuple(
            connection.scalars(
                select(MatchOperationORM.operation_id).where(MatchOperationORM.match_id == seeded.match_id)
            )
        )
        settings_operation_ids = tuple(
            connection.scalars(
                select(SettingsOperationORM.operation_id).where(SettingsOperationORM.guild_id == seeded.guild_id)
            )
        )
        connection.execute(delete(BetORM).where(BetORM.match_id == seeded.match_id))
        connection.execute(delete(DiscordPublicationORM).where(DiscordPublicationORM.guild_id == seeded.guild_id))
        if operation_ids:
            connection.execute(delete(MatchOperationORM).where(MatchOperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        if settings_operation_ids:
            connection.execute(
                delete(SettingsOperationORM).where(SettingsOperationORM.operation_id.in_(settings_operation_ids))
            )
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(settings_operation_ids)))
        if seeded.rating_rule_version_id is not None:
            connection.execute(
                delete(RatingRuleORM).where(RatingRuleORM.rating_rule_version_id == seeded.rating_rule_version_id)
            )
            connection.execute(
                delete(RatingRuleVersionORM).where(RatingRuleVersionORM.id == seeded.rating_rule_version_id)
            )
        connection.execute(delete(MatchEntryORM).where(MatchEntryORM.match_id == seeded.match_id))
        connection.execute(delete(MatchConditionORM).where(MatchConditionORM.match_id == seeded.match_id))
        connection.execute(delete(MatchORM).where(MatchORM.id == seeded.match_id))
        connection.execute(delete(BotGuildSettingORM).where(BotGuildSettingORM.guild_id == seeded.guild_id))
        connection.execute(delete(StadiumCourseORM).where(StadiumCourseORM.id == seeded.course_id))
        connection.execute(delete(StadiumORM).where(StadiumORM.id == seeded.stadium_id))
        connection.execute(delete(UmamusumeORM).where(UmamusumeORM.id.in_(seeded.umamusume_ids)))
        connection.execute(delete(GameAccountORM).where(GameAccountORM.id.in_(seeded.account_ids)))
        connection.execute(delete(PersonaORM).where(PersonaORM.id.in_(seeded.persona_ids)))


def test_opening_transition_publication_audit_and_exact_retry(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_opening_match(migrated_engine, suffix=suffix)
    commands, queries = _services(migrated_engine)
    try:
        choices = queries.search_targets(search=suffix, limit=25)
        assert [(choice.match_id, choice.is_ready) for choice in choices] == [(seeded.match_id, True)]
        target = queries.get_target(match_id=seeded.match_id, guild_id=seeded.guild_id)
        request = _request(seeded, fingerprint=target.state_fingerprint, key=f"open-{suffix}")

        opened = commands.open_betting(request)
        assert commands.open_betting(request) == opened

        with migrated_engine.connect() as connection:
            stored_status = connection.scalar(select(MatchORM.status).where(MatchORM.id == seeded.match_id))
            audit = connection.execute(
                select(MatchOperationORM.before_data, MatchOperationORM.after_data)
                .join(OperationORM, OperationORM.id == MatchOperationORM.operation_id)
                .where(
                    MatchOperationORM.match_id == seeded.match_id,
                    MatchOperationORM.type == "match_betting_opened",
                )
            ).one()
            publication = connection.execute(
                select(
                    DiscordPublicationORM.status,
                    DiscordPublicationORM.target_channel_id,
                    DiscordPublicationORM.event_key,
                    DiscordPublicationORM.payload_json,
                    DiscordPublicationORM.payload_fingerprint,
                ).where(
                    DiscordPublicationORM.destination_kind == "match_announcement",
                    DiscordPublicationORM.source_id == seeded.match_id,
                )
            ).one()
            cursor = connection.execute(
                select(
                    BotGuildSettingORM.match_odds_refresh_mode,
                    BotGuildSettingORM.match_odds_refresh_next_at,
                    BotGuildSettingORM.match_odds_refresh_sequence,
                    BotGuildSettingORM.match_odds_last_projection_fingerprint,
                ).where(BotGuildSettingORM.guild_id == seeded.guild_id)
            ).one()

        assert stored_status == "betting_open"
        assert audit.before_data["status"] == "scheduled"
        assert "rating_rule_coverage" not in audit.before_data
        assert audit.after_data["status"] == "betting_open"
        assert publication.status == "ready"
        assert publication.target_channel_id == seeded.channel_id
        assert publication.event_key == f"match:{seeded.match_id}:betting-open:v1"
        assert len(publication.payload_json["entries"]) == 3
        assert [market["uniform_odds"] for market in publication.payload_json["odds"]["markets"]] == [
            "3.0000",
            "3.0000",
            "1.0000",
        ]
        assert publication.payload_fingerprint == opened.publication.payload_fingerprint
        assert cursor.match_odds_refresh_mode == "normal"
        assert cursor.match_odds_refresh_next_at == (NOW + timedelta(minutes=10)).replace(tzinfo=None)
        assert cursor.match_odds_refresh_sequence == 0
        assert cursor.match_odds_last_projection_fingerprint is None

        delivery = compose_publication_delivery(DatabaseRuntime.from_engine(migrated_engine))
        claimed = delivery.claim_next(retry_delay=timedelta(minutes=5), max_attempts=3)
        assert claimed is not None
        assert claimed.publication_id == opened.publication.publication_id
        assert claimed.destination_kind == MATCH_ANNOUNCEMENT_DESTINATION_KIND
        assert claimed.event_type == MATCH_BETTING_OPENED_EVENT_TYPE
        assert claimed.payload_json == publication.payload_json
    finally:
        _cleanup(migrated_engine, seeded)


@pytest.mark.parametrize(
    ("entry_count", "expected_issue"),
    (
        (1, "Entry가 최소 2명 필요합니다. (현재 1명)"),
        (19, "Entry는 최대 18명까지 허용됩니다. (현재 19명)"),
    ),
)
def test_out_of_range_entry_open_is_zero_write(
    migrated_engine: Engine,
    entry_count: int,
    expected_issue: str,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_opening_match(migrated_engine, suffix=suffix, entry_count=entry_count)
    commands, queries = _services(migrated_engine)
    try:
        choices = queries.search_targets(search=suffix, limit=25)
        assert [(choice.match_id, choice.is_ready) for choice in choices] == [(seeded.match_id, False)]
        target = queries.get_target(match_id=seeded.match_id, guild_id=seeded.guild_id)
        assert target.readiness_issues == (expected_issue,)

        with pytest.raises(MatchBettingOpenIncompleteError, match="최소 2명|최대 18명"):
            commands.open_betting(_request(seeded, fingerprint=target.state_fingerprint, key=f"out-of-range-{suffix}"))

        with migrated_engine.connect() as connection:
            status = connection.scalar(select(MatchORM.status).where(MatchORM.id == seeded.match_id))
            audit_count = connection.scalar(
                select(func.count()).select_from(MatchOperationORM).where(MatchOperationORM.match_id == seeded.match_id)
            )
            publication_count = connection.scalar(
                select(func.count())
                .select_from(DiscordPublicationORM)
                .where(DiscordPublicationORM.guild_id == seeded.guild_id)
            )
            cursor = connection.execute(
                select(
                    BotGuildSettingORM.match_odds_refresh_next_at,
                    BotGuildSettingORM.match_odds_refresh_sequence,
                    BotGuildSettingORM.match_odds_last_projection_fingerprint,
                ).where(BotGuildSettingORM.guild_id == seeded.guild_id)
            ).one()

        assert status == "scheduled"
        assert audit_count == 0
        assert publication_count == 0
        assert cursor.match_odds_refresh_next_at is None
        assert cursor.match_odds_refresh_sequence == 0
        assert cursor.match_odds_last_projection_fingerprint is None
    finally:
        _cleanup(migrated_engine, seeded)


def test_eighteen_entry_open_succeeds(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_opening_match(migrated_engine, suffix=suffix, entry_count=18)
    commands, queries = _services(migrated_engine)
    try:
        target = queries.get_target(match_id=seeded.match_id, guild_id=seeded.guild_id)
        assert target.readiness_issues == ()

        opened = commands.open_betting(
            _request(seeded, fingerprint=target.state_fingerprint, key=f"maximum-entry-{suffix}")
        )

        assert opened.entry_count == 18
        with migrated_engine.connect() as connection:
            assert connection.scalar(select(MatchORM.status).where(MatchORM.id == seeded.match_id)) == "betting_open"
            payload = connection.scalar(
                select(DiscordPublicationORM.payload_json).where(
                    DiscordPublicationORM.source_id == seeded.match_id,
                    DiscordPublicationORM.event_type == MATCH_BETTING_OPENED_EVENT_TYPE,
                )
            )
        assert payload is not None
        assert len(payload["entries"]) == 18
    finally:
        _cleanup(migrated_engine, seeded)


@pytest.mark.parametrize(
    ("rating_rule_version_complete", "covered_converted_ranks", "expected_issue"),
    (
        (False, None, "현재 Rating rule version이 완전하지 않습니다."),
        (True, (1, 2), "현재 Rating rule version에 G1 · Entry 3명 규칙이 완전하지 않습니다."),
    ),
)
def test_unready_current_rating_rules_reject_open_without_writes(
    migrated_engine: Engine,
    rating_rule_version_complete: bool,
    covered_converted_ranks: tuple[int, ...] | None,
    expected_issue: str,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_opening_match(
        migrated_engine,
        suffix=suffix,
        rating_rule_version_complete=rating_rule_version_complete,
        covered_converted_ranks=covered_converted_ranks,
    )
    commands, queries = _services(migrated_engine)
    try:
        choices = queries.search_targets(search=suffix, limit=25)
        assert [(choice.match_id, choice.is_ready) for choice in choices] == [(seeded.match_id, False)]
        target = queries.get_target(match_id=seeded.match_id, guild_id=seeded.guild_id)
        assert target.readiness_issues == (expected_issue,)

        with pytest.raises(MatchBettingOpenIncompleteError, match="Rating rule"):
            commands.open_betting(_request(seeded, fingerprint=target.state_fingerprint, key=f"unready-rules-{suffix}"))

        with migrated_engine.connect() as connection:
            assert connection.scalar(select(MatchORM.status).where(MatchORM.id == seeded.match_id)) == "scheduled"
            assert (
                connection.scalar(
                    select(MatchOperationORM.operation_id).where(MatchOperationORM.match_id == seeded.match_id)
                )
                is None
            )
            assert (
                connection.scalar(
                    select(DiscordPublicationORM.id).where(DiscordPublicationORM.source_id == seeded.match_id)
                )
                is None
            )
            cursor = connection.execute(
                select(
                    BotGuildSettingORM.match_odds_refresh_next_at,
                    BotGuildSettingORM.match_odds_refresh_sequence,
                ).where(BotGuildSettingORM.guild_id == seeded.guild_id)
            ).one()
        assert cursor.match_odds_refresh_next_at is None
        assert cursor.match_odds_refresh_sequence == 0
    finally:
        _cleanup(migrated_engine, seeded)


@pytest.mark.parametrize(
    ("grade", "seed_rating_rule_version"),
    (
        (MatchGrade.LISTED, True),
        (MatchGrade.OP, False),
    ),
)
def test_listed_and_op_open_with_grade_specific_rule_requirements(
    migrated_engine: Engine,
    grade: MatchGrade,
    seed_rating_rule_version: bool,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_opening_match(
        migrated_engine,
        suffix=suffix,
        grade=grade,
        seed_rating_rule_version=seed_rating_rule_version,
    )
    commands, queries = _services(migrated_engine)
    try:
        target = queries.get_target(match_id=seeded.match_id, guild_id=seeded.guild_id)
        assert target.readiness_issues == ()

        opened = commands.open_betting(
            _request(seeded, fingerprint=target.state_fingerprint, key=f"grade-specific-{suffix}")
        )

        assert opened.status.value == "betting_open"
    finally:
        _cleanup(migrated_engine, seeded)


def test_final_open_reloads_current_rating_rule_coverage(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_opening_match(migrated_engine, suffix=suffix)
    commands, queries = _services(migrated_engine)
    newer_version_id: int | None = None
    try:
        previewed = queries.get_target(match_id=seeded.match_id, guild_id=seeded.guild_id)
        assert previewed.readiness_issues == ()
        stored_now = NOW.replace(tzinfo=None)
        with migrated_engine.begin() as connection:
            latest_version = connection.scalar(select(func.max(RatingRuleVersionORM.version_number))) or 0
            newer_version_id = connection.execute(
                RatingRuleVersionORM.__table__.insert().values(
                    version_number=latest_version + 1,
                    source_identifier=f"betting-open-newer-{suffix}",
                    source_checksum="e" * 64,
                    source_sheet_name="G1",
                    source_range="A1:D2",
                    rule_set_checksum="f" * 64,
                    rule_count=2,
                    created_at=stored_now,
                )
            ).inserted_primary_key[0]
            connection.execute(
                RatingRuleORM.__table__.insert(),
                [
                    {
                        "rating_rule_version_id": newer_version_id,
                        "grade": MatchGrade.G1.value,
                        "participant_count": 2,
                        "converted_rank": rank,
                        "base_delta": Decimal(2 - rank),
                        "created_at": stored_now,
                    }
                    for rank in (1, 2)
                ],
            )

        with pytest.raises(MatchBettingOpenIncompleteError, match="Entry 3명 규칙"):
            commands.open_betting(
                _request(seeded, fingerprint=previewed.state_fingerprint, key=f"current-rules-{suffix}")
            )

        current = queries.get_target(match_id=seeded.match_id, guild_id=seeded.guild_id)
        assert current.state_fingerprint == previewed.state_fingerprint
        assert current.readiness_issues == ("현재 Rating rule version에 G1 · Entry 3명 규칙이 완전하지 않습니다.",)
    finally:
        if newer_version_id is not None:
            with migrated_engine.begin() as connection:
                connection.execute(
                    delete(RatingRuleORM).where(RatingRuleORM.rating_rule_version_id == newer_version_id)
                )
                connection.execute(delete(RatingRuleVersionORM).where(RatingRuleVersionORM.id == newer_version_id))
        _cleanup(migrated_engine, seeded)


def test_stale_preview_and_pre_open_bet_are_zero_write(migrated_engine: Engine) -> None:
    stale_suffix = uuid4().hex
    stale_seeded = _seed_opening_match(migrated_engine, suffix=stale_suffix)
    commands, queries = _services(migrated_engine)
    try:
        target = queries.get_target(match_id=stale_seeded.match_id, guild_id=stale_seeded.guild_id)
        with migrated_engine.begin() as connection:
            connection.execute(
                update(MatchConditionORM)
                .where(MatchConditionORM.match_id == stale_seeded.match_id)
                .values(weather="rain")
            )
        with pytest.raises(MatchBettingOpenStaleError):
            commands.open_betting(
                _request(stale_seeded, fingerprint=target.state_fingerprint, key=f"stale-{stale_suffix}")
            )
        with migrated_engine.connect() as connection:
            assert connection.scalar(select(MatchORM.status).where(MatchORM.id == stale_seeded.match_id)) == "scheduled"
            assert (
                connection.scalar(
                    select(DiscordPublicationORM.id).where(DiscordPublicationORM.source_id == stale_seeded.match_id)
                )
                is None
            )
    finally:
        _cleanup(migrated_engine, stale_seeded)

    bet_suffix = uuid4().hex
    bet_seeded = _seed_opening_match(migrated_engine, suffix=bet_suffix)
    commands, queries = _services(migrated_engine)
    try:
        target = queries.get_target(match_id=bet_seeded.match_id, guild_id=bet_seeded.guild_id)
        stored_now = NOW.replace(tzinfo=None)
        with migrated_engine.begin() as connection:
            connection.execute(
                BetORM.__table__.insert().values(
                    match_id=bet_seeded.match_id,
                    persona_id=bet_seeded.persona_ids[0],
                    type="win",
                    selections=[bet_seeded.entry_ids[0]],
                    selection_fingerprint="b" * 64,
                    amount=10,
                    status="active",
                    active_marker=True,
                    created_at=stored_now,
                    updated_at=stored_now,
                )
            )
        with pytest.raises(MatchBettingOpenInvalidSourceError, match="pre-open Bet"):
            commands.open_betting(_request(bet_seeded, fingerprint=target.state_fingerprint, key=f"bet-{bet_suffix}"))
        with migrated_engine.connect() as connection:
            assert connection.scalar(select(MatchORM.status).where(MatchORM.id == bet_seeded.match_id)) == "scheduled"
            assert (
                connection.scalar(
                    select(DiscordPublicationORM.id).where(DiscordPublicationORM.source_id == bet_seeded.match_id)
                )
                is None
            )
    finally:
        _cleanup(migrated_engine, bet_seeded)


def test_setup_edit_and_betting_open_share_one_match_root_serialization(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_opening_match(migrated_engine, suffix=suffix)
    open_commands, open_queries = _services(migrated_engine)
    runtime = DatabaseRuntime.from_engine(migrated_engine)
    setup_commands = MatchSetupCommands(
        CommandRunner(SqlAlchemyMatchSetupUnitOfWorkFactory(runtime.session_factory)),
        clock=lambda: NOW + timedelta(minutes=1),
    )
    setup_queries = MatchStaffSetupQueries(
        QueryRunner(SqlAlchemyMatchStaffSetupQueryUnitOfWorkFactory(runtime.session_factory))
    )
    open_target = open_queries.get_target(match_id=seeded.match_id, guild_id=seeded.guild_id)
    setup_target = setup_queries.get_target(match_id=seeded.match_id)
    assert setup_target is not None
    barrier = Barrier(2)

    def open_match() -> object:
        barrier.wait(timeout=10)
        return open_commands.open_betting(
            _request(seeded, fingerprint=open_target.state_fingerprint, key=f"race-open-{suffix}")
        )

    def edit_match() -> object:
        barrier.wait(timeout=10)
        return setup_commands.update_setup(
            UpdateMatchSetup(
                match_id=seeded.match_id,
                name=f"Edited Opening Match {suffix}",
                description="pre-open edit",
                grade=MatchGrade.G2,
                stadium_course_id=seeded.course_id,
                scheduled_at=SCHEDULED_AT + timedelta(days=1),
                condition=MatchConditionValues(
                    season=MatchSeason.AUTUMN,
                    weather=MatchWeather.RAIN,
                    time_of_day=MatchTimeOfDay.NIGHT,
                    track_condition=MatchTrackCondition.GOOD,
                ),
                expected_state_fingerprint=setup_target.setup.state_fingerprint,
                expected_setup_version=setup_target.setup.setup_version,
                idempotency_key=f"race-edit-{suffix}",
                actor_discord_user_id="123456789",
                guild_id=seeded.guild_id,
                correlation_id=f"race-edit-{suffix}",
                reason="concurrent pre-open edit",
            )
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures: tuple[Future[object], ...] = (executor.submit(open_match), executor.submit(edit_match))
            outcomes: list[object] = []
            for future in futures:
                try:
                    outcomes.append(future.result(timeout=15))
                except (MatchBettingOpenStaleError, MatchSetupUnavailableError) as error:
                    outcomes.append(error)

        assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
        assert sum(isinstance(outcome, Exception) for outcome in outcomes) == 1
        with migrated_engine.connect() as connection:
            status = connection.scalar(select(MatchORM.status).where(MatchORM.id == seeded.match_id))
            setup_audits = connection.scalar(
                select(MatchOperationORM.operation_id).where(
                    MatchOperationORM.match_id == seeded.match_id,
                    MatchOperationORM.type == "match_setup_updated",
                )
            )
            publication_id = connection.scalar(
                select(DiscordPublicationORM.id).where(
                    DiscordPublicationORM.source_kind == "match",
                    DiscordPublicationORM.source_id == seeded.match_id,
                )
            )
        if status == "betting_open":
            assert setup_audits is None
            assert publication_id is not None
        else:
            assert status == "scheduled"
            assert setup_audits is not None
            assert publication_id is None
    finally:
        _cleanup(migrated_engine, seeded)


def test_periodic_odds_due_unchanged_mode_change_and_exact_retry_are_durable(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_opening_match(migrated_engine, suffix=suffix)
    open_commands, open_queries = _services(migrated_engine)
    try:
        target = open_queries.get_target(match_id=seeded.match_id, guild_id=seeded.guild_id)
        open_commands.open_betting(_request(seeded, fingerprint=target.state_fingerprint, key=f"odds-open-{suffix}"))
        stored_now = (NOW + timedelta(minutes=1)).replace(tzinfo=None)
        with migrated_engine.begin() as connection:
            connection.execute(
                BetORM.__table__.insert().values(
                    match_id=seeded.match_id,
                    persona_id=seeded.persona_ids[0],
                    type="win",
                    selections=[seeded.entry_ids[0]],
                    selection_fingerprint="d" * 64,
                    amount=10,
                    status="active",
                    active_marker=True,
                    created_at=stored_now,
                    updated_at=stored_now,
                )
            )

        due_commands, _ = _odds_services(migrated_engine, now=NOW + timedelta(minutes=10))
        first = due_commands.refresh_due(guild_id=seeded.guild_id)
        assert first.publication is not None
        assert first.mode is MatchOddsRefreshMode.NORMAL
        assert first.open_match_count == 1

        unchanged_commands, _ = _odds_services(migrated_engine, now=NOW + timedelta(minutes=20))
        unchanged = unchanged_commands.refresh_due(guild_id=seeded.guild_id)
        assert unchanged.publication is None
        assert unchanged.cursor_changed is True

        live_at = NOW + timedelta(minutes=21)
        mode_commands, mode_queries = _odds_services(migrated_engine, now=live_at)
        command = ChangeMatchOddsRefreshMode(
            guild_id=seeded.guild_id,
            desired_mode=MatchOddsRefreshMode.LIVE,
            actor_discord_user_id="123456789",
            idempotency_key=f"odds-live-{suffix}",
            correlation_id=f"odds-live-{suffix}",
        )
        changed = mode_commands.change_mode(command)
        assert changed.publication is not None
        assert mode_commands.change_mode(command) == changed
        status = mode_queries.get_status(guild_id=seeded.guild_id)

        with migrated_engine.connect() as connection:
            odds_publications = connection.execute(
                select(
                    DiscordPublicationORM.event_key,
                    DiscordPublicationORM.event_type,
                    DiscordPublicationORM.source_id,
                    DiscordPublicationORM.payload_json,
                )
                .where(
                    DiscordPublicationORM.guild_id == seeded.guild_id,
                    DiscordPublicationORM.event_type == MATCH_ODDS_REFRESH_EVENT_TYPE,
                )
                .order_by(DiscordPublicationORM.source_id)
            ).all()
            setting = connection.execute(
                select(
                    BotGuildSettingORM.match_odds_refresh_mode,
                    BotGuildSettingORM.match_odds_refresh_next_at,
                    BotGuildSettingORM.match_odds_refresh_sequence,
                    BotGuildSettingORM.match_odds_last_projection_fingerprint,
                ).where(BotGuildSettingORM.guild_id == seeded.guild_id)
            ).one()
            mode_audits = connection.execute(
                select(SettingsOperationORM.type, SettingsOperationORM.before_data, SettingsOperationORM.after_data)
                .where(SettingsOperationORM.guild_id == seeded.guild_id)
                .order_by(SettingsOperationORM.operation_id)
            ).all()

        assert [row.event_key for row in odds_publications] == [
            "match-odds-refresh:1:v1",
            "match-odds-refresh:2:v1",
        ]
        assert all(row.event_type == MATCH_ODDS_REFRESH_EVENT_TYPE for row in odds_publications)
        assert [row.source_id for row in odds_publications] == [1, 2]
        assert odds_publications[0].payload_json["matches"][0]["match_id"] == seeded.match_id
        assert odds_publications[0].payload_json["coverage"]["mode"] == "normal"
        assert odds_publications[1].payload_json["coverage"]["mode"] == "live"
        assert setting.match_odds_refresh_mode == "live"
        assert setting.match_odds_refresh_next_at == (live_at + timedelta(minutes=1)).replace(tzinfo=None)
        assert setting.match_odds_refresh_sequence == 2
        assert len(setting.match_odds_last_projection_fingerprint) == 64
        assert status.mode is MatchOddsRefreshMode.LIVE
        assert status.open_match_count == 1
        assert len(mode_audits) == 1
        assert mode_audits[0].type == "match_odds_refresh_mode_changed"
        assert mode_audits[0].before_data["cursor"]["mode"] == "normal"
        assert mode_audits[0].after_data["mode"] == "live"
    finally:
        _cleanup(migrated_engine, seeded)


def test_concurrent_periodic_due_checks_create_one_logical_refresh(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_opening_match(migrated_engine, suffix=suffix)
    open_commands, open_queries = _services(migrated_engine)
    try:
        target = open_queries.get_target(match_id=seeded.match_id, guild_id=seeded.guild_id)
        open_commands.open_betting(
            _request(seeded, fingerprint=target.state_fingerprint, key=f"concurrent-odds-open-{suffix}")
        )
        barrier = Barrier(2)

        def refresh_after_barrier() -> object:
            commands, _ = _odds_services(migrated_engine, now=NOW + timedelta(minutes=10))
            barrier.wait(timeout=10)
            return commands.refresh_due(guild_id=seeded.guild_id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = tuple(executor.submit(refresh_after_barrier) for _ in range(2))
            results = tuple(future.result(timeout=15) for future in futures)

        assert sum(result.publication is not None for result in results) == 1  # type: ignore[attr-defined]
        with migrated_engine.connect() as connection:
            publication_count = connection.scalar(
                select(func.count(DiscordPublicationORM.id)).where(
                    DiscordPublicationORM.guild_id == seeded.guild_id,
                    DiscordPublicationORM.event_type == MATCH_ODDS_REFRESH_EVENT_TYPE,
                )
            )
            sequence = connection.scalar(
                select(BotGuildSettingORM.match_odds_refresh_sequence).where(
                    BotGuildSettingORM.guild_id == seeded.guild_id
                )
            )
        assert publication_count == 1
        assert sequence == 1
    finally:
        _cleanup(migrated_engine, seeded)


def test_periodic_odds_rejects_inconsistent_active_marker_without_cursor_write(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_opening_match(migrated_engine, suffix=suffix)
    open_commands, open_queries = _services(migrated_engine)
    try:
        target = open_queries.get_target(match_id=seeded.match_id, guild_id=seeded.guild_id)
        open_commands.open_betting(
            _request(seeded, fingerprint=target.state_fingerprint, key=f"malformed-odds-open-{suffix}")
        )
        stored_now = (NOW + timedelta(minutes=1)).replace(tzinfo=None)
        with migrated_engine.begin() as connection:
            connection.execute(
                BetORM.__table__.insert().values(
                    match_id=seeded.match_id,
                    persona_id=seeded.persona_ids[0],
                    type="win",
                    selections=[seeded.entry_ids[0]],
                    selection_fingerprint="e" * 64,
                    amount=10,
                    status="active",
                    active_marker=None,
                    created_at=stored_now,
                    updated_at=stored_now,
                )
            )

        commands, _ = _odds_services(migrated_engine, now=NOW + timedelta(minutes=10))
        with pytest.raises(MatchOddsPublicationInvalidSourceError):
            commands.refresh_due(guild_id=seeded.guild_id)

        with migrated_engine.connect() as connection:
            cursor = connection.execute(
                select(
                    BotGuildSettingORM.match_odds_refresh_next_at,
                    BotGuildSettingORM.match_odds_refresh_sequence,
                ).where(BotGuildSettingORM.guild_id == seeded.guild_id)
            ).one()
            odds_publication = connection.scalar(
                select(DiscordPublicationORM.id).where(
                    DiscordPublicationORM.guild_id == seeded.guild_id,
                    DiscordPublicationORM.event_type == MATCH_ODDS_REFRESH_EVENT_TYPE,
                )
            )
        assert cursor.match_odds_refresh_next_at == (NOW + timedelta(minutes=10)).replace(tzinfo=None)
        assert cursor.match_odds_refresh_sequence == 0
        assert odds_publication is None
    finally:
        _cleanup(migrated_engine, seeded)
