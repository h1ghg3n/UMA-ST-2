from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    AccountRegistrationOperationAudit,
    AccountRegistrationRequest,
    CirclePointAccount,
    CirclePointTransaction,
    DiscordAccount,
    DiscordPublication,
    DiscordPublicationAudit,
    GameAccount,
    GameEvent,
    Persona,
    Race,
    RaceEntry,
    SheetImportRecord,
    SheetImportRun,
    Win5Entry,
    Win5Judgement,
    Win5OperationAudit,
    Win5Pick,
    Win5Result,
    Win5Round,
    Win5RoundRace,
    Win5Score,
    Win5ScoreEvent,
    Win5Season,
)
from umacircle_bot.services.current_win5_replay_provenance import CURRENT_WIN5_REPLAY_IMPORT_KIND
from umacircle_bot.sheets.current_win5_replay_manifest import CurrentWin5ReplayManifest

DEFERRED_MATCH_RECONCILIATION_VERSION = 1


def build_circle_point_signature_payload(session: Session) -> dict[str, object]:
    return {
        "wallets": _all_rows(session, CirclePointAccount),
        "transactions": _all_rows(session, CirclePointTransaction),
    }


def build_win5_signature_payload(
    session: Session,
    *,
    manifest: CurrentWin5ReplayManifest,
) -> tuple[dict[str, object], int]:
    participant_identity = _participant_identity_payload(session, manifest=manifest)
    registration_request_ids = select(AccountRegistrationRequest.id).where(
        AccountRegistrationRequest.requester_discord_user_id.in_(
            participant.discord_user_id
            for participant in manifest.participants
            if participant.provenance_kind == "account_registration"
        )
    )
    payload = {
        "participant_identity": participant_identity,
        "registration_requests": _filtered_rows(
            session,
            AccountRegistrationRequest,
            AccountRegistrationRequest.id.in_(registration_request_ids),
        ),
        "registration_audits": _filtered_rows(
            session,
            AccountRegistrationOperationAudit,
            AccountRegistrationOperationAudit.account_registration_request_id.in_(registration_request_ids),
        ),
        "seasons": _all_rows(session, Win5Season),
        "rounds": _all_rows(session, Win5Round),
        "round_races": _all_rows(session, Win5RoundRace),
        "races": _filtered_rows(session, Race, Race.race_kind == "win5"),
        "race_entries": _race_entry_rows(session),
        "game_events": _win5_game_event_rows(session),
        "entries": _all_rows(session, Win5Entry),
        "picks": _all_rows(session, Win5Pick),
        "results": _all_rows(session, Win5Result),
        "judgements": _all_rows(session, Win5Judgement),
        "scores": _all_rows(session, Win5Score),
        "score_events": _all_rows(session, Win5ScoreEvent),
        "operation_audits": _all_rows(session, Win5OperationAudit),
        "publications": _win5_publication_rows(session),
        "publication_audits": _win5_publication_audit_rows(session),
        "replay_runs": _filtered_rows(
            session,
            SheetImportRun,
            SheetImportRun.import_kind == CURRENT_WIN5_REPLAY_IMPORT_KIND,
        ),
        "replay_records": _replay_record_rows(session),
    }
    return payload, len(participant_identity["discord_accounts"])


def build_payload_signature(kind: str, payload: Mapping[str, object]) -> str:
    canonical = json.dumps(
        {
            "signature_version": DEFERRED_MATCH_RECONCILIATION_VERSION,
            "kind": kind,
            "payload": payload,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _participant_identity_payload(
    session: Session,
    *,
    manifest: CurrentWin5ReplayManifest,
) -> dict[str, object]:
    discord_accounts = tuple(
        session.scalars(
            select(DiscordAccount)
            .where(DiscordAccount.discord_user_id.in_(manifest.participant_discord_user_ids))
            .order_by(DiscordAccount.discord_user_id)
        )
    )
    discord_by_user_id = {account.discord_user_id: account for account in discord_accounts}
    persona_ids: set[str] = set()
    participant_game_account_ids: set[int] = set()
    for participant in manifest.participants:
        discord = discord_by_user_id.get(participant.discord_user_id)
        persona = session.get(Persona, discord.persona_id) if discord is not None and discord.persona_id else None
        if persona is None:
            continue
        persona_ids.add(persona.id)
        matching_account_ids = tuple(
            session.scalars(
                select(GameAccount.id).where(
                    GameAccount.persona_id == persona.id,
                    GameAccount.uma_pid == participant.uma_pid,
                    GameAccount.identity_status == "confirmed_identity",
                )
            )
        )
        if len(matching_account_ids) == 1:
            participant_game_account_ids.add(matching_account_ids[0])
    return {
        "discord_accounts": _filtered_rows(
            session,
            DiscordAccount,
            DiscordAccount.discord_user_id.in_(manifest.participant_discord_user_ids),
        ),
        "personas": _filtered_rows(session, Persona, Persona.id.in_(persona_ids)) if persona_ids else [],
        # WIN5 is Persona-owned. Unrelated peer GameAccounts may be attached by
        # deferred Match mapping, so freeze only the manifest PID provenance.
        "game_accounts": (
            _filtered_rows(session, GameAccount, GameAccount.id.in_(participant_game_account_ids))
            if participant_game_account_ids
            else []
        ),
    }


def _all_rows(session: Session, model: type[object]) -> list[dict[str, object]]:
    table = model.__table__  # type: ignore[attr-defined]
    columns = tuple(table.columns)
    statement = select(*columns).order_by(*table.primary_key.columns)
    return _statement_rows(session, columns=columns, statement=statement)


def _filtered_rows(session: Session, model: type[object], *criteria: object) -> list[dict[str, object]]:
    table = model.__table__  # type: ignore[attr-defined]
    columns = tuple(table.columns)
    statement = select(*columns).where(*criteria).order_by(*table.primary_key.columns)
    return _statement_rows(session, columns=columns, statement=statement)


def _race_entry_rows(session: Session) -> list[dict[str, object]]:
    table = RaceEntry.__table__
    columns = tuple(table.columns)
    statement = (
        select(*columns)
        .select_from(table.join(Race.__table__, Race.id == RaceEntry.race_id))
        .where(Race.race_kind == "win5")
        .order_by(*table.primary_key.columns)
    )
    return _statement_rows(session, columns=columns, statement=statement)


def _win5_game_event_rows(session: Session) -> list[dict[str, object]]:
    event_ids = select(Win5Result.event_id).where(Win5Result.event_id.is_not(None))
    return _filtered_rows(session, GameEvent, GameEvent.id.in_(event_ids))


def _win5_publication_rows(session: Session) -> list[dict[str, object]]:
    return _filtered_rows(
        session,
        DiscordPublication,
        DiscordPublication.destination_kind == "win5_announcement",
    )


def _win5_publication_audit_rows(session: Session) -> list[dict[str, object]]:
    table = DiscordPublicationAudit.__table__
    publication = DiscordPublication.__table__
    columns = tuple(table.columns)
    statement = (
        select(*columns)
        .select_from(table.join(publication, publication.c.id == table.c.publication_id))
        .where(publication.c.destination_kind == "win5_announcement")
        .order_by(*table.primary_key.columns)
    )
    return _statement_rows(session, columns=columns, statement=statement)


def _replay_record_rows(session: Session) -> list[dict[str, object]]:
    record = SheetImportRecord.__table__
    run = SheetImportRun.__table__
    columns = tuple(record.columns)
    statement = (
        select(*columns)
        .select_from(record.join(run, run.c.id == record.c.import_run_id))
        .where(run.c.import_kind == CURRENT_WIN5_REPLAY_IMPORT_KIND)
        .order_by(*record.primary_key.columns)
    )
    return _statement_rows(session, columns=columns, statement=statement)


def _statement_rows(
    session: Session,
    *,
    columns: Sequence[object],
    statement: object,
) -> list[dict[str, object]]:
    return [
        {column.name: _json_value(value) for column, value in zip(columns, row, strict=True)}
        for row in session.execute(statement)
    ]


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value
