"""Provider-independent WIN5 scored-Round publication projections."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from uma_st2.domain.publication import PublicationStatus
from uma_st2.domain.win5 import Win5JudgementOutcome, Win5RoundType, Win5SubmissionTier

from .intents import PublicationIntent, publication_payload_fingerprint

WIN5_PUBLICATION_PAYLOAD_SCHEMA_VERSION: Final = 1
WIN5_VOID_PUBLICATION_PAYLOAD_SCHEMA_VERSION: Final = 2
WIN5_ANNOUNCEMENT_DESTINATION_KIND: Final = "win5_announcement"
WIN5_ROUND_PUBLICATION_SOURCE_KIND: Final = "win5_round"
WIN5_ROUND_RESULT_EVENT_TYPE: Final = "win5_round_scored"
WIN5_HALL_OF_FAME_EVENT_TYPE: Final = "win5_hall_of_fame_updated"


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _require_bounded_string(
    value: str | None,
    *,
    field_name: str,
    max_length: int,
    optional: bool = False,
) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not value or len(value) > max_length:
        qualifier = "optional " if optional else ""
        raise ValueError(f"{field_name} must be a non-empty {qualifier}string no longer than {max_length} characters.")


@dataclass(frozen=True, slots=True)
class Win5PublicationDestination:
    """Current guild setting used to choose the initial delivery state."""

    guild_id: str
    announcements_enabled: bool
    target_channel_id: str | None

    def __post_init__(self) -> None:
        _require_bounded_string(self.guild_id, field_name="guild_id", max_length=32)
        if not isinstance(self.announcements_enabled, bool):
            raise ValueError("announcements_enabled must be a boolean.")
        _require_bounded_string(
            self.target_channel_id,
            field_name="target_channel_id",
            max_length=32,
            optional=True,
        )

    @property
    def initial_status(self) -> PublicationStatus:
        if not self.announcements_enabled:
            return PublicationStatus.SUPPRESSED
        if self.target_channel_id is None:
            return PublicationStatus.AWAITING_CHANNEL
        return PublicationStatus.READY

    @property
    def snapshotted_channel_id(self) -> str | None:
        return self.target_channel_id if self.initial_status == PublicationStatus.READY else None


@dataclass(frozen=True, slots=True)
class Win5PublicationResultPlacement:
    """One authoritative Result row enriched only with display metadata."""

    result_id: int
    position: int
    gate_number: int
    race_entry_id: int | None = None
    horse_name: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.result_id, field_name="result_id")
        _require_positive_int(self.position, field_name="position")
        _require_positive_int(self.gate_number, field_name="gate_number")
        if self.race_entry_id is not None:
            _require_positive_int(self.race_entry_id, field_name="race_entry_id")
        _require_bounded_string(
            self.horse_name,
            field_name="horse_name",
            max_length=100,
            optional=True,
        )

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "result_id": self.result_id,
            "position": self.position,
            "gate_number": self.gate_number,
        }
        if self.race_entry_id is not None:
            payload["race_entry_id"] = self.race_entry_id
        if self.horse_name is not None:
            payload["horse_name"] = self.horse_name
        return payload


@dataclass(frozen=True, slots=True)
class Win5PublicationRace:
    """One canonical Race and its complete authoritative publication Result."""

    race_id: int
    race_name: str
    placements: tuple[Win5PublicationResultPlacement, ...]
    void_reason: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.race_id, field_name="race_id")
        _require_bounded_string(self.race_name, field_name="race_name", max_length=200)
        canonical = tuple(sorted(self.placements, key=lambda placement: placement.position))
        if len({placement.position for placement in canonical}) != len(canonical):
            raise ValueError("Race publication placements must be unique by position.")
        _require_bounded_string(
            self.void_reason,
            field_name="void_reason",
            max_length=255,
            optional=True,
        )
        if (self.void_reason is None) == (not canonical):
            raise ValueError("Race publication requires either placements or an explicit void reason.")
        object.__setattr__(self, "placements", canonical)

    @property
    def is_void(self) -> bool:
        return self.void_reason is not None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "race_id": self.race_id,
            "race_name": self.race_name,
            "placements": [placement.to_payload() for placement in self.placements],
        }
        if self.void_reason is not None:
            payload["void_reason"] = self.void_reason
        return payload


@dataclass(frozen=True, slots=True)
class Win5PublicationPersona:
    """Persona display snapshot used by public hit records."""

    persona_id: str
    display_name: str

    def __post_init__(self) -> None:
        _require_bounded_string(self.persona_id, field_name="persona_id", max_length=36)
        _require_bounded_string(self.display_name, field_name="display_name", max_length=100)


@dataclass(frozen=True, slots=True)
class Win5ScoredSubmissionPublicationSource:
    """Stored-scoring facts needed to decide hit and Hall-of-Fame projection."""

    submission_id: int
    persona_id: str
    tier: Win5SubmissionTier
    season_score_delta: int
    top1_score_delta: int
    outcomes: tuple[Win5JudgementOutcome, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_positive_int(self.submission_id, field_name="submission_id")
        _require_bounded_string(self.persona_id, field_name="persona_id", max_length=36)
        object.__setattr__(self, "tier", Win5SubmissionTier(self.tier))
        for field_name in ("season_score_delta", "top1_score_delta"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer.")
        object.__setattr__(
            self,
            "outcomes",
            tuple(Win5JudgementOutcome(outcome) for outcome in self.outcomes),
        )


@dataclass(frozen=True, slots=True)
class Win5RoundPublicationSource:
    """Canonical display and Result snapshot read inside the scoring UoW."""

    destination: Win5PublicationDestination
    season_id: int
    season_name: str
    round_id: int
    round_name: str
    round_type: Win5RoundType
    races: tuple[Win5PublicationRace, ...]
    personas: tuple[Win5PublicationPersona, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_positive_int(self.season_id, field_name="season_id")
        _require_bounded_string(self.season_name, field_name="season_name", max_length=100)
        _require_positive_int(self.round_id, field_name="round_id")
        _require_bounded_string(self.round_name, field_name="round_name", max_length=100)
        object.__setattr__(self, "round_type", Win5RoundType(self.round_type))
        canonical_races = tuple(sorted(self.races, key=lambda race: race.race_id))
        if not canonical_races or len({race.race_id for race in canonical_races}) != len(canonical_races):
            raise ValueError("Round publication Races must be non-empty and unique by Race ID.")
        object.__setattr__(self, "races", canonical_races)
        if self.round_type == Win5RoundType.NORMAL and any(race.is_void for race in canonical_races):
            raise ValueError("Normal Round publication cannot contain a void Race.")
        if self.round_type == Win5RoundType.SPECIAL and all(race.is_void for race in canonical_races):
            raise ValueError("An all-void Special Round cannot produce a scored publication.")
        canonical_personas = tuple(sorted(self.personas, key=lambda persona: persona.persona_id))
        if len({persona.persona_id for persona in canonical_personas}) != len(canonical_personas):
            raise ValueError("Round publication Personas must be unique by Persona ID.")
        object.__setattr__(self, "personas", canonical_personas)


def _round_identity_payload(source: Win5RoundPublicationSource) -> dict[str, object]:
    return {
        "season": {"id": source.season_id, "name": source.season_name},
        "round": {
            "id": source.round_id,
            "name": source.round_name,
            "type": source.round_type.value,
        },
    }


def _make_intent(
    *,
    source: Win5RoundPublicationSource,
    event_type: str,
    event_key: str,
    payload: dict[str, object],
) -> PublicationIntent:
    return PublicationIntent(
        guild_id=source.destination.guild_id,
        destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
        event_type=event_type,
        event_key=event_key,
        source_kind=WIN5_ROUND_PUBLICATION_SOURCE_KIND,
        source_id=source.round_id,
        target_channel_id=source.destination.snapshotted_channel_id,
        payload_json=payload,
        payload_fingerprint=publication_payload_fingerprint(payload),
        status=source.destination.initial_status,
    )


def build_win5_scored_publication_intents(
    *,
    source: Win5RoundPublicationSource,
    scored_submissions: tuple[Win5ScoredSubmissionPublicationSource, ...],
) -> tuple[PublicationIntent, ...]:
    """Build the Round summary and optional coalesced Hall-of-Fame intent."""

    canonical_submissions = tuple(sorted(scored_submissions, key=lambda item: (item.persona_id, item.submission_id)))
    if len({item.submission_id for item in canonical_submissions}) != len(canonical_submissions):
        raise ValueError("Scored publication Submissions must be unique.")
    persona_by_id = {persona.persona_id: persona for persona in source.personas}
    if set(persona_by_id) != {item.persona_id for item in canonical_submissions}:
        raise ValueError("Scored publication Persona snapshots must exactly match scored Submissions.")

    hit_records = [
        {
            "submission_id": item.submission_id,
            "persona_id": item.persona_id,
            "display_name": persona_by_id[item.persona_id].display_name,
            "tier": item.tier.value,
            "season_score_delta": item.season_score_delta,
            "top1_score_delta": item.top1_score_delta,
        }
        for item in canonical_submissions
        if item.season_score_delta > 0
    ]
    identity = _round_identity_payload(source)
    payload_schema_version = (
        WIN5_VOID_PUBLICATION_PAYLOAD_SCHEMA_VERSION
        if any(race.is_void for race in source.races)
        else WIN5_PUBLICATION_PAYLOAD_SCHEMA_VERSION
    )
    result_payload: dict[str, object] = {
        "schema_version": payload_schema_version,
        "publication_type": "win5_round_result",
        **identity,
        "results": [race.to_payload() for race in source.races],
        "hits": hit_records,
        "no_hits": not hit_records,
    }
    intents = [
        _make_intent(
            source=source,
            event_type=WIN5_ROUND_RESULT_EVENT_TYPE,
            event_key=f"win5-round:{source.round_id}:scored-result:v1",
            payload=result_payload,
        )
    ]

    hall_records = [
        {
            "submission_id": item.submission_id,
            "persona_id": item.persona_id,
            "display_name": persona_by_id[item.persona_id].display_name,
            "tier": item.tier.value,
        }
        for item in canonical_submissions
        if source.round_type == Win5RoundType.NORMAL
        and item.tier == Win5SubmissionTier.TOP5
        and len(item.outcomes) == 5
        and all(outcome == Win5JudgementOutcome.EXACT for outcome in item.outcomes)
    ]
    if hall_records:
        hall_payload: dict[str, object] = {
            "schema_version": WIN5_PUBLICATION_PAYLOAD_SCHEMA_VERSION,
            "publication_type": "win5_hall_of_fame_update",
            **identity,
            "results": [race.to_payload() for race in source.races],
            "records": hall_records,
        }
        intents.append(
            _make_intent(
                source=source,
                event_type=WIN5_HALL_OF_FAME_EVENT_TYPE,
                event_key=f"win5-round:{source.round_id}:hall-of-fame:v1",
                payload=hall_payload,
            )
        )
    return tuple(intents)
