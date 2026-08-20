from __future__ import annotations

import re
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from umacircle_bot.db.models import (
    Bet,
    CirclePointAccount,
    CirclePointTransaction,
    DiscordAccount,
    GameAccount,
    Persona,
    Race,
    SheetImportRecord,
    SheetImportRun,
)
from umacircle_bot.domain.circle_point_provenance import (
    ACCOUNT_REGISTRATION_APPROVAL_TRANSACTION_SOURCE,
    ACCOUNT_REGISTRATION_INITIAL_GRANT_TRANSACTION_TYPE,
)
from umacircle_bot.domain.errors import LegacyImportError
from umacircle_bot.domain.identity import IdentityStatus
from umacircle_bot.runtime_preflight import EXPECTED_ALEMBIC_HEAD, EXPECTED_ROOM_POINT_SCALE
from umacircle_bot.services.circle_point_integrity import count_historical_bet_owner_audit_mismatches
from umacircle_bot.services.current_win5_replay_registration import registration_provenance_matches
from umacircle_bot.services.fresh_win5_launch import WIN5_OPERATIONAL_MODELS
from umacircle_bot.services.legacy_import import (
    LEGACY_IDENTITY_POINT_IMPORT_KIND,
    LEGACY_IDENTITY_POINT_RECORD_TYPE,
)
from umacircle_bot.services.legacy_ledger_import import LEGACY_LEDGER_IMPORT_KIND
from umacircle_bot.services.legacy_payout_correction import LEGACY_PAYOUT_CORRECTION_IMPORT_KIND
from umacircle_bot.services.win5_first_baseline import (
    WIN5_FIRST_BASELINE_IMPORT_KIND,
    WIN5_FIRST_BASELINE_MANIFEST_VERSION,
    WIN5_FIRST_BASELINE_RECORD_TYPE,
    WIN5_FIRST_BASELINE_TRANSACTION_SOURCE,
    WIN5_FIRST_BASELINE_TRANSACTION_TYPE,
)
from umacircle_bot.sheets.current_win5_replay_manifest import (
    CurrentWin5ReplayManifest,
    CurrentWin5ReplayParticipant,
)

WIN5_FIRST_PREFLIGHT_MANIFEST_VERSION = 1
_BOT_COMMIT_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def build_win5_first_launch_preflight(
    session: Session,
    *,
    manifest: CurrentWin5ReplayManifest,
    bot_commit: str,
    runtime_bot_commit: str,
) -> dict[str, object]:
    """Build the read-only pre-epoch report for the selected WIN5-first lane."""

    normalized_source = manifest.room_match_source_identifier
    normalized_source_checksum = manifest.room_match_source_checksum
    normalized_baseline_checksum = manifest.win5_first_baseline_checksum
    normalized_commit = _normalize_bot_commit(bot_commit)
    normalized_runtime_commit = _normalize_bot_commit(runtime_bot_commit)
    participant_ids = _normalize_participant_ids(manifest.participant_discord_user_ids)
    participant_by_discord_id = {participant.discord_user_id: participant for participant in manifest.participants}
    participants = tuple(participant_by_discord_id[discord_user_id] for discord_user_id in participant_ids)
    source_identity_count = manifest.expected_source_identity_count
    source_participant_count = manifest.expected_source_participant_count
    opening_total = manifest.target_point_totals.room_match_baseline
    registration_total = manifest.target_point_totals.registration
    if source_participant_count > len(participant_ids):
        raise LegacyImportError("source participant count exceeds the participant manifest")
    if source_identity_count < source_participant_count:
        raise LegacyImportError("source identity count is smaller than source participant count")
    if session.new or session.dirty or session.deleted:
        raise LegacyImportError("WIN5-first launch preflight requires a clean session")

    revision = session.scalar(text("SELECT version_num FROM alembic_version"))
    point_scale = session.scalar(text("SELECT scale_version FROM room_point_scale_state WHERE id = 1"))
    errors: list[str] = []
    if revision != EXPECTED_ALEMBIC_HEAD:
        errors.append("unexpected_alembic_revision")
    if point_scale != EXPECTED_ROOM_POINT_SCALE:
        errors.append("unexpected_room_point_scale")
    if normalized_runtime_commit != normalized_commit:
        errors.append("bot_commit_mismatch")

    baseline, baseline_errors, source_persona_ids, source_owners = _baseline_report(
        session,
        source_identifier=normalized_source,
        source_checksum=normalized_source_checksum,
        expected_baseline_checksum=normalized_baseline_checksum,
        expected_source_identity_count=source_identity_count,
        expected_opening_total=opening_total,
    )
    errors.extend(baseline_errors)

    participant_checks, participant_errors, participant_persona_ids = _participant_report(
        session,
        participants=participants,
        source_owners=source_owners,
    )
    errors.extend(participant_errors)
    source_participant_persona_ids = source_persona_ids & participant_persona_ids
    if len(source_participant_persona_ids) != source_participant_count:
        errors.append("source_participant_count_mismatch")

    population, population_errors = _population_report(
        session,
        source_persona_ids=source_persona_ids,
        participant_persona_ids=participant_persona_ids,
        expected_source_identity_count=source_identity_count,
        expected_source_participant_count=source_participant_count,
    )
    errors.extend(population_errors)

    room_points, room_point_errors = _point_report(
        session,
        baseline_transaction_ids=set(baseline["transaction_ids"]),
        source_persona_ids=source_persona_ids,
        participant_persona_ids=participant_persona_ids,
        expected_opening_total=opening_total,
        expected_registration_total=registration_total,
        expected_registration_count=len(participant_ids) - source_participant_count,
    )
    errors.extend(room_point_errors)

    forbidden_import_counts = {
        import_kind: int(
            session.scalar(
                select(func.count()).select_from(SheetImportRun).where(SheetImportRun.import_kind == import_kind)
            )
            or 0
        )
        for import_kind in (LEGACY_LEDGER_IMPORT_KIND, LEGACY_PAYOUT_CORRECTION_IMPORT_KIND)
    }
    if any(forbidden_import_counts.values()):
        errors.append("historical_monetary_import_present")

    win5_table_counts = {
        model.__tablename__: int(session.scalar(select(func.count()).select_from(model)) or 0)
        for model in WIN5_OPERATIONAL_MODELS
    }
    win5_race_count = int(session.scalar(select(func.count()).select_from(Race).where(Race.race_kind == "win5")) or 0)
    win5_reward_transaction_count = int(
        session.scalar(
            select(func.count())
            .select_from(CirclePointTransaction)
            .where(
                or_(
                    CirclePointTransaction.type == "win5_reward",
                    CirclePointTransaction.source == "win5_score",
                )
            )
        )
        or 0
    )
    if any(win5_table_counts.values()) or win5_race_count or win5_reward_transaction_count:
        errors.append("win5_epoch_not_empty")

    return {
        "manifest_version": WIN5_FIRST_PREFLIGHT_MANIFEST_VERSION,
        "lane": "win5_first",
        "phase": "pre_epoch_launch",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_identifier": normalized_source,
        "source_checksum": normalized_source_checksum,
        "replay_manifest_checksum": manifest.manifest_checksum,
        "replay_file_checksum": manifest.file_checksum,
        "bot_commit": normalized_commit,
        "runtime_bot_commit": normalized_runtime_commit,
        "expected_alembic_revision": EXPECTED_ALEMBIC_HEAD,
        "alembic_revision": revision,
        "room_point_scale": point_scale,
        "baseline": {key: value for key, value in baseline.items() if key != "transaction_ids"},
        "participant_manifest": {
            "participant_count": len(participant_ids),
            "source_participant_count": source_participant_count,
            "participant_checksum": _participant_checksum(participant_ids),
        },
        "participant_checks": participant_checks,
        "population": population,
        "room_points": room_points,
        "forbidden_monetary_import_counts": forbidden_import_counts,
        "win5_table_counts": win5_table_counts,
        "win5_race_count": win5_race_count,
        "win5_reward_transaction_count": win5_reward_transaction_count,
        "ready_to_launch": not errors,
        "errors": tuple(dict.fromkeys(errors)),
        "warnings": (),
    }


def _baseline_report(
    session: Session,
    *,
    source_identifier: str,
    source_checksum: str,
    expected_baseline_checksum: str,
    expected_source_identity_count: int,
    expected_opening_total: int,
) -> tuple[dict[str, object], tuple[str, ...], set[str], dict[str, tuple[str, int]]]:
    errors: list[str] = []
    identity_runs = tuple(
        session.scalars(
            select(SheetImportRun).where(
                SheetImportRun.import_kind == LEGACY_IDENTITY_POINT_IMPORT_KIND,
                SheetImportRun.source_type == "xlsx",
                SheetImportRun.source_identifier == source_identifier,
                SheetImportRun.source_checksum == source_checksum,
                SheetImportRun.status == "completed",
                SheetImportRun.finished_at.is_not(None),
            )
        )
    )
    baseline_runs = tuple(
        session.scalars(
            select(SheetImportRun).where(
                SheetImportRun.import_kind == WIN5_FIRST_BASELINE_IMPORT_KIND,
                SheetImportRun.source_type == "xlsx",
                SheetImportRun.source_identifier == source_identifier,
                SheetImportRun.source_checksum == source_checksum,
                SheetImportRun.status == "completed",
                SheetImportRun.finished_at.is_not(None),
            )
        )
    )
    if len(identity_runs) != 1:
        errors.append("identity_import_run_count")
    if len(baseline_runs) != 1:
        errors.append("baseline_import_run_count")
    identity_run = identity_runs[0] if len(identity_runs) == 1 else None
    baseline_run = baseline_runs[0] if len(baseline_runs) == 1 else None
    identity_records = (
        tuple(
            session.scalars(
                select(SheetImportRecord)
                .where(SheetImportRecord.import_run_id == identity_run.id)
                .order_by(SheetImportRecord.source_row_number)
            )
        )
        if identity_run is not None
        else ()
    )
    baseline_records = (
        tuple(
            session.scalars(
                select(SheetImportRecord)
                .where(SheetImportRecord.import_run_id == baseline_run.id)
                .order_by(SheetImportRecord.source_row_number)
            )
        )
        if baseline_run is not None
        else ()
    )
    if len(identity_records) != expected_source_identity_count:
        errors.append("identity_import_record_count")
    if len(baseline_records) != expected_source_identity_count:
        errors.append("baseline_import_record_count")

    identity_by_id = {record.id: record for record in identity_records}
    transaction_ids: list[int] = []
    source_persona_ids: set[str] = set()
    source_owners: dict[str, tuple[str, int]] = {}
    record_error_count = 0
    for record in baseline_records:
        detail = record.detail_json if isinstance(record.detail_json, dict) else {}
        identity_record = identity_by_id.get(detail.get("identity_import_record_id"))
        transaction = (
            session.get(CirclePointTransaction, record.target_entity_id)
            if record.target_entity_id is not None
            else None
        )
        persona_id = detail.get("persona_id")
        identity_source_key = detail.get("identity_source_key")
        game_account_id = detail.get("game_account_id")
        valid = (
            record.record_type == WIN5_FIRST_BASELINE_RECORD_TYPE
            and record.status == "applied"
            and record.target_entity_type == "room_point_transaction"
            and isinstance(persona_id, str)
            and bool(persona_id)
            and isinstance(identity_source_key, str)
            and bool(identity_source_key)
            and isinstance(game_account_id, int)
            and not isinstance(game_account_id, bool)
            and identity_record is not None
            and identity_record.record_type == LEGACY_IDENTITY_POINT_RECORD_TYPE
            and identity_record.status == "applied"
            and identity_record.source_key == identity_source_key
            and transaction is not None
            and transaction.type == WIN5_FIRST_BASELINE_TRANSACTION_TYPE
            and transaction.source == WIN5_FIRST_BASELINE_TRANSACTION_SOURCE
            and transaction.persona_id == persona_id
            and transaction.game_account_id == game_account_id
            and transaction.amount == detail.get("target_balance")
            and transaction.idempotency_key == detail.get("transaction_idempotency_key")
            and detail.get("manifest_version") == WIN5_FIRST_BASELINE_MANIFEST_VERSION
            and detail.get("room_point_scale") == EXPECTED_ROOM_POINT_SCALE
        )
        if not valid or persona_id in source_persona_ids or identity_source_key in source_owners:
            record_error_count += 1
            continue
        source_persona_ids.add(persona_id)
        source_owners[identity_source_key] = (persona_id, game_account_id)
        transaction_ids.append(transaction.id)
    if record_error_count:
        errors.append("baseline_record_provenance_mismatch")

    calculated_checksum = _baseline_checksum(
        baseline_records,
        source_identifier=source_identifier,
        source_checksum=source_checksum,
    )
    summary = (
        baseline_run.summary_json if baseline_run is not None and isinstance(baseline_run.summary_json, dict) else {}
    )
    summary_matches = (
        summary.get("manifest_version") == WIN5_FIRST_BASELINE_MANIFEST_VERSION
        and summary.get("identity_count") == expected_source_identity_count
        and summary.get("wallet_count") == expected_source_identity_count
        and summary.get("transaction_count") == expected_source_identity_count
        and summary.get("target_total") == expected_opening_total
        and summary.get("room_point_scale") == EXPECTED_ROOM_POINT_SCALE
        and summary.get("baseline_checksum") == expected_baseline_checksum
        and calculated_checksum == expected_baseline_checksum
    )
    if not summary_matches:
        errors.append("baseline_summary_mismatch")
    baseline_transaction_total = int(
        session.scalar(
            select(func.coalesce(func.sum(CirclePointTransaction.amount), 0)).where(
                CirclePointTransaction.id.in_(transaction_ids)
            )
        )
        or 0
    )
    if len(transaction_ids) != expected_source_identity_count or baseline_transaction_total != expected_opening_total:
        errors.append("baseline_transaction_total_mismatch")

    return (
        {
            "identity_import_run_id": identity_run.id if identity_run is not None else None,
            "baseline_import_run_id": baseline_run.id if baseline_run is not None else None,
            "source_identity_count": len(source_persona_ids),
            "baseline_record_count": len(baseline_records),
            "baseline_transaction_count": len(transaction_ids),
            "opening_total": baseline_transaction_total,
            "baseline_checksum": calculated_checksum,
            "summary_matches": summary_matches,
            "transaction_ids": tuple(sorted(transaction_ids)),
        },
        tuple(errors),
        source_persona_ids,
        source_owners,
    )


def _participant_report(
    session: Session,
    *,
    participants: tuple[CurrentWin5ReplayParticipant, ...],
    source_owners: dict[str, tuple[str, int]],
) -> tuple[tuple[dict[str, object], ...], tuple[str, ...], set[str]]:
    checks: list[dict[str, object]] = []
    errors: list[str] = []
    persona_ids: set[str] = set()
    source_persona_ids = {persona_id for persona_id, _account_id in source_owners.values()}
    for participant in participants:
        discord_user_id = participant.discord_user_id
        discord = session.scalar(select(DiscordAccount).where(DiscordAccount.discord_user_id == discord_user_id))
        persona = session.get(Persona, discord.persona_id) if discord is not None and discord.persona_id else None
        accounts = (
            tuple(
                session.scalars(
                    select(GameAccount).where(GameAccount.persona_id == persona.id).order_by(GameAccount.id)
                )
            )
            if persona is not None
            else ()
        )
        expected_pid = participant.uma_pid
        matching_accounts = tuple(
            account
            for account in accounts
            if account.identity_status == IdentityStatus.CONFIRMED.value and account.uma_pid == expected_pid
        )
        account = matching_accounts[0] if len(matching_accounts) == 1 else None
        identity_source_key = participant.identity_source_key
        wallet = (
            session.scalar(select(CirclePointAccount).where(CirclePointAccount.persona_id == persona.id))
            if persona is not None
            else None
        )
        error: str | None = None
        if discord is None:
            error = "discord_account_missing"
        elif persona is None or persona.status != "active":
            error = "active_persona_missing"
        elif persona.id in persona_ids:
            error = "participant_persona_duplicate"
        elif account is None:
            error = "reviewed_game_account_missing"
        elif wallet is None:
            error = "participant_point_account_missing"
        elif identity_source_key is not None and source_owners.get(identity_source_key) != (persona.id, account.id):
            error = "reviewed_source_owner_mismatch"
        elif participant.provenance_kind == "account_registration" and (
            persona.id in source_persona_ids
            or not registration_provenance_matches(
                session,
                participant=participant,
                persona=persona,
                account=account,
            )
        ):
            error = "registration_provenance_mismatch"
        if persona is not None:
            persona_ids.add(persona.id)
        checks.append(
            {
                "discord_user_id": discord_user_id,
                "persona_id": persona.id if persona is not None else None,
                "game_account_id": account.id if account is not None else None,
                "game_account_count": len(accounts),
                "reviewed_uma_pid": expected_pid,
                "identity_source_key": identity_source_key,
                "ready": error is None,
                "error": error,
            }
        )
        if error is not None:
            errors.append(f"participant:{discord_user_id}:{error}")
    return tuple(checks), tuple(errors), persona_ids


def _population_report(
    session: Session,
    *,
    source_persona_ids: set[str],
    participant_persona_ids: set[str],
    expected_source_identity_count: int,
    expected_source_participant_count: int,
) -> tuple[dict[str, int], tuple[str, ...]]:
    errors: list[str] = []
    expected_non_source_participant_count = len(participant_persona_ids) - expected_source_participant_count
    expected_total = expected_source_identity_count + expected_non_source_participant_count
    counts = {
        "discord_account_count": int(session.scalar(select(func.count()).select_from(DiscordAccount)) or 0),
        "persona_count": int(session.scalar(select(func.count()).select_from(Persona)) or 0),
        "game_account_count": int(session.scalar(select(func.count()).select_from(GameAccount)) or 0),
        "wallet_count": int(session.scalar(select(func.count()).select_from(CirclePointAccount)) or 0),
        "active_persona_count": int(
            session.scalar(select(func.count()).select_from(Persona).where(Persona.status == "active")) or 0
        ),
    }
    if (
        any(counts[key] != expected_total for key in ("discord_account_count", "persona_count", "wallet_count"))
        or counts["game_account_count"] < expected_total
    ):
        errors.append("unexpected_identity_population")

    active_persona_ids = set(session.scalars(select(Persona.id).where(Persona.status == "active")))
    if active_persona_ids != participant_persona_ids:
        errors.append("active_participant_set_mismatch")
    source_nonparticipant_ids = source_persona_ids - participant_persona_ids
    source_nonparticipants = (
        tuple(session.scalars(select(Persona).where(Persona.id.in_(source_nonparticipant_ids))))
        if source_nonparticipant_ids
        else ()
    )
    nonparticipant_account_error_count = 0
    for persona in source_nonparticipants:
        accounts = tuple(session.scalars(select(GameAccount).where(GameAccount.persona_id == persona.id)))
        if (
            persona.status != "inactive"
            or not accounts
            or any(
                account.identity_status != IdentityStatus.PENDING.value or account.uma_pid is not None
                for account in accounts
            )
        ):
            nonparticipant_account_error_count += 1
    if len(source_nonparticipants) != expected_source_identity_count - expected_source_participant_count:
        errors.append("source_nonparticipant_count_mismatch")
    if nonparticipant_account_error_count:
        errors.append("source_nonparticipant_state_mismatch")
    counts.update(
        {
            "source_identity_count": len(source_persona_ids),
            "source_nonparticipant_count": len(source_nonparticipants),
            "participant_count": len(participant_persona_ids),
            "source_participant_count": len(source_persona_ids & participant_persona_ids),
            "nonparticipant_account_error_count": nonparticipant_account_error_count,
        }
    )
    return counts, tuple(errors)


def _point_report(
    session: Session,
    *,
    baseline_transaction_ids: set[int],
    source_persona_ids: set[str],
    participant_persona_ids: set[str],
    expected_opening_total: int,
    expected_registration_total: int,
    expected_registration_count: int,
) -> tuple[dict[str, object], tuple[str, ...]]:
    errors: list[str] = []
    wallets = tuple(session.scalars(select(CirclePointAccount).order_by(CirclePointAccount.persona_id)))
    transactions = tuple(session.scalars(select(CirclePointTransaction).order_by(CirclePointTransaction.id)))
    registration_transactions = tuple(tx for tx in transactions if tx.id not in baseline_transaction_ids)
    new_participant_persona_ids = participant_persona_ids - source_persona_ids
    invalid_registration_count = sum(
        tx.type != ACCOUNT_REGISTRATION_INITIAL_GRANT_TRANSACTION_TYPE
        or tx.source != ACCOUNT_REGISTRATION_APPROVAL_TRANSACTION_SOURCE
        or tx.persona_id not in new_participant_persona_ids
        or tx.amount <= 0
        for tx in registration_transactions
    )
    registration_total = sum(tx.amount for tx in registration_transactions)
    if (
        len(registration_transactions) != expected_registration_count
        or invalid_registration_count
        or registration_total != expected_registration_total
    ):
        errors.append("registration_point_baseline_mismatch")

    transaction_totals = {
        str(row.persona_id): int(row[1] or 0)
        for row in session.execute(
            select(CirclePointTransaction.persona_id, func.sum(CirclePointTransaction.amount))
            .group_by(CirclePointTransaction.persona_id)
            .order_by(CirclePointTransaction.persona_id)
        )
    }
    wallet_balances = {wallet.persona_id: wallet.balance for wallet in wallets}
    mismatched_persona_ids = tuple(
        persona_id
        for persona_id in sorted(set(wallet_balances) | set(transaction_totals))
        if wallet_balances.get(persona_id, 0) != transaction_totals.get(persona_id, 0)
    )
    wallet_total = sum(wallet_balances.values())
    transaction_total = sum(transaction.amount for transaction in transactions)
    expected_total = expected_opening_total + expected_registration_total
    if wallet_total != expected_total or transaction_total != expected_total or mismatched_persona_ids:
        errors.append("room_point_reconciliation_mismatch")
    transaction_orphan_count = int(
        session.scalar(
            select(func.count())
            .select_from(CirclePointTransaction)
            .outerjoin(Persona, Persona.id == CirclePointTransaction.persona_id)
            .outerjoin(CirclePointAccount, CirclePointAccount.persona_id == CirclePointTransaction.persona_id)
            .outerjoin(GameAccount, GameAccount.id == CirclePointTransaction.game_account_id)
            .where(
                or_(
                    Persona.id.is_(None),
                    CirclePointAccount.id.is_(None),
                    GameAccount.id.is_(None),
                )
            )
        )
        or 0
    )
    bet_orphan_count = int(
        session.scalar(
            select(func.count())
            .select_from(Bet)
            .outerjoin(Persona, Persona.id == Bet.persona_id)
            .outerjoin(CirclePointAccount, CirclePointAccount.persona_id == Bet.persona_id)
            .outerjoin(GameAccount, GameAccount.id == Bet.game_account_id)
            .where(
                or_(
                    Persona.id.is_(None),
                    CirclePointAccount.id.is_(None),
                    GameAccount.id.is_(None),
                )
            )
        )
        or 0
    )
    bet_ledger_provenance_mismatch_count = int(
        session.scalar(
            select(func.count())
            .select_from(CirclePointTransaction)
            .outerjoin(Bet, Bet.id == CirclePointTransaction.related_bet_id)
            .where(
                CirclePointTransaction.related_bet_id.is_not(None),
                or_(
                    Bet.id.is_(None),
                    Bet.persona_id != CirclePointTransaction.persona_id,
                    Bet.game_account_id != CirclePointTransaction.game_account_id,
                ),
            )
        )
        or 0
    )
    historical_bet_owner_audit_mismatch_count = count_historical_bet_owner_audit_mismatches(session)
    provenance_error_count = (
        transaction_orphan_count
        + bet_orphan_count
        + bet_ledger_provenance_mismatch_count
        + historical_bet_owner_audit_mismatch_count
    )
    if provenance_error_count:
        errors.append("room_point_transaction_owner_mismatch")
    return (
        {
            "wallet_count": len(wallets),
            "transaction_count": len(transactions),
            "opening_transaction_count": len(baseline_transaction_ids),
            "registration_transaction_count": len(registration_transactions),
            "opening_total": expected_opening_total,
            "registration_total": registration_total,
            "wallet_total": wallet_total,
            "transaction_total": transaction_total,
            "mismatched_persona_count": len(mismatched_persona_ids),
            "transaction_owner_mismatch_count": provenance_error_count,
            "transaction_orphan_count": transaction_orphan_count,
            "bet_orphan_count": bet_orphan_count,
            "bet_ledger_provenance_mismatch_count": bet_ledger_provenance_mismatch_count,
            "historical_bet_owner_audit_mismatch_count": historical_bet_owner_audit_mismatch_count,
        },
        tuple(errors),
    )


def _baseline_checksum(
    records: tuple[SheetImportRecord, ...],
    *,
    source_identifier: str,
    source_checksum: str,
) -> str:
    payload = f"win5-first-opening-baseline-v1\n{source_identifier}\n{source_checksum}\n" + "\n".join(
        f"{record.source_key}:{record.row_fingerprint}" for record in records
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _normalize_participant_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    if not values:
        raise LegacyImportError("WIN5-first launch preflight requires at least one participant")
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise LegacyImportError("participant Discord user ID must be text")
        item = value.strip()
        if not item.isascii() or not item.isdigit() or not 1 <= len(item) <= 32:
            raise LegacyImportError("participant Discord user ID must contain 1 to 32 ASCII digits")
        if item in normalized:
            raise LegacyImportError("participant Discord user IDs must be unique")
        normalized.add(item)
    return tuple(sorted(normalized))


def _participant_checksum(participant_ids: tuple[str, ...]) -> str:
    payload = "fresh-win5-participants-v1\n" + "\n".join(participant_ids)
    return sha256(payload.encode("utf-8")).hexdigest()


def _normalize_bot_commit(value: str) -> str:
    if not isinstance(value, str):
        raise LegacyImportError("bot commit must be text")
    normalized = value.strip().lower()
    if _BOT_COMMIT_PATTERN.fullmatch(normalized) is None:
        raise LegacyImportError("bot commit must be a full 40 or 64 character hexadecimal revision")
    return normalized
