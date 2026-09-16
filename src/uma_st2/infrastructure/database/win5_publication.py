"""SQLAlchemy source projection and durable insert for WIN5 publications."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from uma_st2.application.publication import (
    PublicationIntent,
    Win5PublicationDestination,
    Win5PublicationPersona,
    Win5PublicationRace,
    Win5PublicationResultPlacement,
    Win5RoundPublicationSource,
)
from uma_st2.domain.win5 import Win5RoundSourceKind, Win5RoundType

from .datetime_codec import to_database_utc
from .orm import (
    BotGuildSettingORM,
    DiscordPublicationORM,
    PersonaORM,
    Win5RaceEntryORM,
    Win5RaceORM,
    Win5ResultORM,
    Win5RoundORM,
    Win5SeasonORM,
)


class SqlAlchemyWin5PublicationStore:
    """Read immutable scoring display facts and append logical outbox rows."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def load_scored_round_source(
        self,
        *,
        guild_id: str,
        round_id: int,
        persona_ids: tuple[str, ...],
    ) -> Win5RoundPublicationSource:
        setting = self._session.get(BotGuildSettingORM, guild_id)
        destination = Win5PublicationDestination(
            guild_id=guild_id,
            announcements_enabled=True if setting is None else setting.win5_announcements_enabled,
            target_channel_id=None if setting is None else setting.win5_announcement_channel_id,
        )

        identity = self._session.execute(
            select(
                Win5SeasonORM.id.label("season_id"),
                Win5SeasonORM.name.label("season_name"),
                Win5RoundORM.id.label("round_id"),
                Win5RoundORM.name.label("round_name"),
                Win5RoundORM.type.label("round_type"),
            )
            .join(Win5SeasonORM, Win5SeasonORM.id == Win5RoundORM.season_id)
            .where(
                Win5RoundORM.id == round_id,
                Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
            )
        ).one_or_none()
        if identity is None:
            raise ValueError("WIN5 publication Round or Season is missing.")
        round_type = Win5RoundType(identity.round_type)

        race_rows = self._session.execute(
            select(
                Win5RaceORM.id,
                Win5RaceORM.name,
                Win5RaceORM.void_reason,
                Win5RaceORM.voided_at,
            )
            .where(Win5RaceORM.round_id == round_id)
            .order_by(Win5RaceORM.id)
        ).all()
        if any((race.void_reason is None) != (race.voided_at is None) for race in race_rows):
            raise ValueError("WIN5 publication Race has an incomplete void fact.")
        race_ids = tuple(row.id for row in race_rows)
        placements_by_race: dict[int, list[Win5PublicationResultPlacement]] = defaultdict(list)
        if race_ids and round_type == Win5RoundType.NORMAL:
            result_rows = self._session.execute(
                select(
                    Win5ResultORM.id.label("result_id"),
                    Win5ResultORM.race_id,
                    Win5ResultORM.position,
                    Win5ResultORM.race_entry_id,
                    Win5RaceEntryORM.gate_number,
                    Win5RaceEntryORM.name.label("horse_name"),
                )
                .join(Win5RaceEntryORM, Win5RaceEntryORM.id == Win5ResultORM.race_entry_id)
                .where(Win5ResultORM.race_id.in_(race_ids))
                .order_by(Win5ResultORM.race_id, Win5ResultORM.position)
            ).all()
        elif race_ids:
            result_rows = self._session.execute(
                select(
                    Win5ResultORM.id.label("result_id"),
                    Win5ResultORM.race_id,
                    Win5ResultORM.position,
                    Win5ResultORM.race_entry_id,
                    Win5ResultORM.gate_number,
                    Win5RaceEntryORM.name.label("horse_name"),
                )
                .outerjoin(
                    Win5RaceEntryORM,
                    and_(
                        Win5RaceEntryORM.race_id == Win5ResultORM.race_id,
                        Win5RaceEntryORM.gate_number == Win5ResultORM.gate_number,
                    ),
                )
                .where(Win5ResultORM.race_id.in_(race_ids))
                .order_by(Win5ResultORM.race_id, Win5ResultORM.position)
            ).all()
        else:
            result_rows = ()

        for row in result_rows:
            if row.gate_number is None:
                raise ValueError("WIN5 publication Result has no authoritative gate number.")
            placements_by_race[row.race_id].append(
                Win5PublicationResultPlacement(
                    result_id=row.result_id,
                    position=row.position,
                    gate_number=row.gate_number,
                    race_entry_id=row.race_entry_id,
                    horse_name=row.horse_name,
                )
            )

        canonical_persona_ids = tuple(sorted(set(persona_ids)))
        persona_rows = (
            self._session.execute(
                select(PersonaORM.id, PersonaORM.display_name)
                .where(PersonaORM.id.in_(canonical_persona_ids))
                .order_by(PersonaORM.id)
            ).all()
            if canonical_persona_ids
            else ()
        )
        return Win5RoundPublicationSource(
            destination=destination,
            season_id=identity.season_id,
            season_name=identity.season_name,
            round_id=identity.round_id,
            round_name=identity.round_name,
            round_type=round_type,
            races=tuple(
                Win5PublicationRace(
                    race_id=race.id,
                    race_name=race.name,
                    placements=tuple(placements_by_race[race.id]),
                    void_reason=race.void_reason,
                )
                for race in race_rows
            ),
            personas=tuple(
                Win5PublicationPersona(
                    persona_id=persona.id,
                    display_name=persona.display_name,
                )
                for persona in persona_rows
            ),
        )

    def add_intents(
        self,
        *,
        intents: tuple[PublicationIntent, ...],
        created_at: datetime,
    ) -> None:
        stored_created_at = to_database_utc(created_at, field_name="created_at")
        self._session.add_all(
            DiscordPublicationORM(
                guild_id=intent.guild_id,
                destination_kind=intent.destination_kind,
                event_type=intent.event_type,
                event_key=intent.event_key,
                source_kind=intent.source_kind,
                source_id=intent.source_id,
                target_channel_id=intent.target_channel_id,
                payload_json=intent.payload_json,
                payload_fingerprint=intent.payload_fingerprint,
                status=intent.status.value,
                attempt_count=0,
                discord_message_id=None,
                last_error_code=None,
                failure_stage=None,
                attempt_started_at=None,
                published_at=None,
                created_at=stored_created_at,
                updated_at=stored_created_at,
            )
            for intent in intents
        )
