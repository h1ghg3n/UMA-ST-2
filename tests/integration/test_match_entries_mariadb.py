"""MariaDB atomic replacement, audit, exact retry, and stale evidence for Match Entries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.match import (
    CreateMatch,
    MatchConditionValues,
    MatchCreationCommands,
    MatchEntryCommands,
    MatchEntrySearchLine,
    MatchEntrySelection,
    MatchEntryStaleError,
    MatchStaffEntryQueries,
    ReplaceMatchEntries,
)
from uma_st2.domain.match import (
    MatchGrade,
    MatchSeason,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
)
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyMatchCreationUnitOfWorkFactory,
    SqlAlchemyMatchEntryUnitOfWorkFactory,
    SqlAlchemyMatchStaffEntryQueryUnitOfWorkFactory,
)
from uma_st2.infrastructure.database.orm import (
    GameAccountORM,
    MatchConditionORM,
    MatchEntryORM,
    MatchOperationORM,
    MatchORM,
    OperationORM,
    PersonaORM,
    StadiumCourseORM,
    StadiumORM,
    UmamusumeORM,
    UmamusumeVariantORM,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 28, 5, 0, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class SeededEntryMatch:
    match_id: int
    creation_operation_id: int
    stadium_id: int
    course_id: int
    persona_ids: tuple[str, str]
    account_ids: tuple[int, int]
    pids: tuple[str, str]
    umamusume_ids: tuple[int, int]
    variant_id: int


def _seed_entry_match(engine: Engine, *, suffix: str) -> SeededEntryMatch:
    runtime = DatabaseRuntime.from_engine(engine)
    created_at = NOW - timedelta(days=1)
    stored_now = created_at.replace(tzinfo=None)
    external_base = int(suffix[:12], 16)
    persona_ids = (str(uuid4()), str(uuid4()))
    pids = (f"1{suffix[:15]}", f"2{suffix[:15]}")
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
                    game_region="kr",
                    uma_pid=pid,
                    nickname=f"Account {index} {suffix}",
                    affiliation=f"Team {index}",
                    created_at=stored_now,
                    updated_at=stored_now,
                )
            ).inserted_primary_key[0]
            for index, (persona_id, pid) in enumerate(zip(persona_ids, pids, strict=True), start=1)
        )
        umamusume_ids = tuple(
            connection.execute(
                UmamusumeORM.__table__.insert().values(
                    external_id=external_base + 10 + index,
                    name_jp=f"Horse JP {index} {suffix}",
                    name_ko=f"말 {index} {suffix}",
                    created_at=stored_now,
                    updated_at=stored_now,
                )
            ).inserted_primary_key[0]
            for index in range(1, 3)
        )
        variant_id = connection.execute(
            UmamusumeVariantORM.__table__.insert().values(
                umamusume_id=umamusume_ids[1],
                external_id=external_base + 20,
                name_jp=f"Variant JP {suffix}",
                name_ko=f"의상 말 2 {suffix}",
                release_date=None,
                created_at=stored_now,
                updated_at=stored_now,
            )
        ).inserted_primary_key[0]
        stadium_id = connection.execute(
            StadiumORM.__table__.insert().values(
                external_id=external_base + 30,
                name_jp=f"Tokyo {suffix}",
                name_ko=f"도쿄 {suffix}",
                created_at=stored_now,
                updated_at=stored_now,
            )
        ).inserted_primary_key[0]
        course_id = connection.execute(
            StadiumCourseORM.__table__.insert().values(
                stadium_id=stadium_id,
                external_id=external_base + 31,
                surface="turf",
                distance=2400,
                direction="left",
                layout="standard",
                created_at=stored_now,
                updated_at=stored_now,
            )
        ).inserted_primary_key[0]

    creation = MatchCreationCommands(
        CommandRunner(SqlAlchemyMatchCreationUnitOfWorkFactory(runtime.session_factory)),
        clock=lambda: created_at,
    ).create_match(
        CreateMatch(
            name=f"Native Entry Match {suffix}",
            description=None,
            grade=MatchGrade.G1,
            stadium_course_id=course_id,
            scheduled_at=SCHEDULED_AT,
            condition=MatchConditionValues(
                season=MatchSeason.AUTUMN,
                weather=MatchWeather.SUNNY,
                time_of_day=MatchTimeOfDay.DAY,
                track_condition=MatchTrackCondition.FIRM,
            ),
            idempotency_key=f"match-create-entry-{suffix}",
            actor_discord_user_id="123456789",
            guild_id="987654321",
            correlation_id=f"create-entry-{suffix}",
        )
    )
    with engine.connect() as connection:
        creation_operation_id = connection.scalar(
            select(MatchOperationORM.operation_id).where(
                MatchOperationORM.match_id == creation.snapshot.match_id,
                MatchOperationORM.type == "match_created",
            )
        )
    assert creation_operation_id is not None
    return SeededEntryMatch(
        match_id=creation.snapshot.match_id,
        creation_operation_id=creation_operation_id,
        stadium_id=stadium_id,
        course_id=course_id,
        persona_ids=persona_ids,
        account_ids=account_ids,
        pids=pids,
        umamusume_ids=umamusume_ids,
        variant_id=variant_id,
    )


def _services(engine: Engine, *, clock: datetime = NOW) -> tuple[MatchEntryCommands, MatchStaffEntryQueries]:
    runtime = DatabaseRuntime.from_engine(engine)
    commands = MatchEntryCommands(
        CommandRunner(SqlAlchemyMatchEntryUnitOfWorkFactory(runtime.session_factory)),
        clock=lambda: clock,
    )
    queries = MatchStaffEntryQueries(
        QueryRunner(SqlAlchemyMatchStaffEntryQueryUnitOfWorkFactory(runtime.session_factory))
    )
    return commands, queries


def _cleanup(engine: Engine, seeded: SeededEntryMatch) -> None:
    with engine.begin() as connection:
        operation_ids = tuple(
            connection.scalars(
                select(MatchOperationORM.operation_id).where(MatchOperationORM.match_id == seeded.match_id)
            )
        )
        connection.execute(delete(MatchEntryORM).where(MatchEntryORM.match_id == seeded.match_id))
        connection.execute(delete(MatchConditionORM).where(MatchConditionORM.match_id == seeded.match_id))
        if operation_ids:
            connection.execute(delete(MatchOperationORM).where(MatchOperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(delete(MatchORM).where(MatchORM.id == seeded.match_id))
        connection.execute(delete(StadiumCourseORM).where(StadiumCourseORM.id == seeded.course_id))
        connection.execute(delete(StadiumORM).where(StadiumORM.id == seeded.stadium_id))
        connection.execute(delete(UmamusumeVariantORM).where(UmamusumeVariantORM.id == seeded.variant_id))
        connection.execute(delete(UmamusumeORM).where(UmamusumeORM.id.in_(seeded.umamusume_ids)))
        connection.execute(delete(GameAccountORM).where(GameAccountORM.id.in_(seeded.account_ids)))
        connection.execute(delete(PersonaORM).where(PersonaORM.id.in_(seeded.persona_ids)))


def test_complete_entry_replacement_query_audit_exact_retry_and_stale(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_entry_match(migrated_engine, suffix=suffix)
    commands, queries = _services(migrated_engine)
    try:
        candidate_draft = queries.prepare_candidates(
            match_id=seeded.match_id,
            lines=(
                MatchEntrySearchLine(f"Account 1 {suffix}"),
                MatchEntrySearchLine(f"Account 2 {suffix}"),
            ),
        )
        assert [row.accounts[0].id for row in candidate_draft.rows] == list(seeded.account_ids)
        assert seeded.variant_id in {item.umamusume_variant_id for item in candidate_draft.characters}
        initial_draft = queries.prepare_replacement(
            match_id=seeded.match_id,
            selections=(
                MatchEntrySelection(seeded.account_ids[0], seeded.umamusume_ids[0]),
                MatchEntrySelection(seeded.account_ids[1], seeded.umamusume_ids[1], seeded.variant_id),
            ),
        )
        initial_request = ReplaceMatchEntries(
            match_id=seeded.match_id,
            entries=initial_draft.command_entries,
            expected_roster_fingerprint=initial_draft.current.roster_fingerprint,
            idempotency_key=f"entry-initial-{suffix}",
            actor_discord_user_id="123456789",
            guild_id="987654321",
            correlation_id=f"entry-initial-{suffix}",
        )

        initial = commands.replace_entries(initial_request)
        assert commands.replace_entries(initial_request) == initial
        assert [entry.entry_number for entry in initial.snapshot.entries] == [1, 2]
        assert initial.snapshot.entries[1].umamusume_variant_id == seeded.variant_id

        replacement_draft = queries.prepare_replacement(
            match_id=seeded.match_id,
            selections=(
                MatchEntrySelection(seeded.account_ids[1], seeded.umamusume_ids[1]),
                MatchEntrySelection(seeded.account_ids[0], seeded.umamusume_ids[0]),
            ),
        )
        replacement_request = ReplaceMatchEntries(
            match_id=seeded.match_id,
            entries=replacement_draft.command_entries,
            expected_roster_fingerprint=replacement_draft.current.roster_fingerprint,
            idempotency_key=f"entry-replacement-{suffix}",
            actor_discord_user_id="123456789",
            guild_id="987654321",
            correlation_id=f"entry-replacement-{suffix}",
            reason="공식 명단 정정",
        )
        replacement_commands, _ = _services(migrated_engine, clock=NOW + timedelta(minutes=1))
        replaced = replacement_commands.replace_entries(replacement_request)
        assert replacement_commands.replace_entries(replacement_request) == replaced

        with pytest.raises(MatchEntryStaleError):
            replacement_commands.replace_entries(
                ReplaceMatchEntries(
                    match_id=seeded.match_id,
                    entries=initial_draft.command_entries,
                    expected_roster_fingerprint=initial_draft.current.roster_fingerprint,
                    idempotency_key=f"entry-stale-{suffix}",
                    actor_discord_user_id="123456789",
                )
            )

        with migrated_engine.connect() as connection:
            stored_entries = connection.execute(
                select(
                    MatchEntryORM.entry_number,
                    MatchEntryORM.game_account_id,
                    MatchEntryORM.owner_at_event_persona_id,
                    MatchEntryORM.umamusume_id,
                    MatchEntryORM.umamusume_variant_id,
                )
                .where(MatchEntryORM.match_id == seeded.match_id)
                .order_by(MatchEntryORM.entry_number)
            ).all()
            audits = connection.execute(
                select(
                    MatchOperationORM.before_data,
                    MatchOperationORM.after_data,
                    OperationORM.reason,
                )
                .join(OperationORM, OperationORM.id == MatchOperationORM.operation_id)
                .where(
                    MatchOperationORM.match_id == seeded.match_id,
                    MatchOperationORM.type == "match_entries_replaced",
                )
                .order_by(MatchOperationORM.operation_id)
            ).all()

        assert [row.entry_number for row in stored_entries] == [1, 2]
        assert [row.game_account_id for row in stored_entries] == [seeded.account_ids[1], seeded.account_ids[0]]
        assert [row.owner_at_event_persona_id for row in stored_entries] == [
            seeded.persona_ids[1],
            seeded.persona_ids[0],
        ]
        assert len(audits) == 2
        assert audits[0].before_data["entries"] == []
        assert len(audits[1].before_data["entries"]) == 2
        assert audits[1].reason == "공식 명단 정정"
        serialized_audit = json.dumps([audit.after_data for audit in audits], ensure_ascii=False)
        assert all(pid not in serialized_audit for pid in seeded.pids)
    finally:
        _cleanup(migrated_engine, seeded)


def test_complete_entry_replacement_preserves_two_characters_for_one_game_account(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_entry_match(migrated_engine, suffix=suffix)
    commands, queries = _services(migrated_engine)
    try:
        draft = queries.prepare_replacement(
            match_id=seeded.match_id,
            selections=(
                MatchEntrySelection(seeded.account_ids[0], seeded.umamusume_ids[0]),
                MatchEntrySelection(seeded.account_ids[0], seeded.umamusume_ids[1], seeded.variant_id),
            ),
        )
        result = commands.replace_entries(
            ReplaceMatchEntries(
                match_id=seeded.match_id,
                entries=draft.command_entries,
                expected_roster_fingerprint=draft.current.roster_fingerprint,
                idempotency_key=f"entry-duplicate-account-{suffix}",
                actor_discord_user_id="123456789",
                guild_id="987654321",
                correlation_id=f"entry-duplicate-account-{suffix}",
            )
        )

        assert [entry.game_account_id for entry in result.snapshot.entries] == [
            seeded.account_ids[0],
            seeded.account_ids[0],
        ]
        assert [entry.umamusume_id for entry in result.snapshot.entries] == list(seeded.umamusume_ids)
        assert result.snapshot.entries[1].umamusume_variant_id == seeded.variant_id
    finally:
        _cleanup(migrated_engine, seeded)
