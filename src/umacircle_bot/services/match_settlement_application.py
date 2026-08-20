from __future__ import annotations

from dataclasses import dataclass

from umacircle_bot.domain.errors import MatchResultError
from umacircle_bot.services.application import run_application_command, run_application_query
from umacircle_bot.services.guild_discord_settings import get_guild_discord_settings
from umacircle_bot.services.match_odds_snapshots import (
    ConfirmMatchOddsSnapshotCommand,
    MatchOddsInput,
    confirm_match_odds_snapshot,
)
from umacircle_bot.services.match_result_notifications import (
    MatchResultNotificationDTO,
    enqueue_match_result_notifications,
)
from umacircle_bot.services.match_results import (
    MarkMatchResultPublicationFailedCommand,
    MarkMatchResultPublicationSentCommand,
    MarkMatchResultPublicationUnknownCommand,
    MatchResultOperationDTO,
    PublishMatchResultCommand,
    get_room_match_result,
    mark_match_result_publication_failed,
    mark_match_result_publication_sent,
    mark_match_result_publication_unknown,
    publish_match_result,
)
from umacircle_bot.services.match_settlement import (
    ConfirmMatchSettlementCommand,
    MatchSettlementOperationDTO,
    confirm_match_settlement,
)
from umacircle_bot.services.match_settlement_rollback import (
    MatchSettlementRollbackDTO,
    RollbackMatchSettlementCommand,
    rollback_confirmed_match_settlement,
)


@dataclass(frozen=True, slots=True)
class ConfirmMatchSettlementApplicationCommand:
    race_id: int
    odds: tuple[MatchOddsInput, ...]
    reason: str | None
    actor_discord_user_id: str
    interaction_id: str


@dataclass(frozen=True, slots=True)
class RollbackMatchSettlementApplicationCommand:
    race_id: int
    reason: str
    actor_discord_user_id: str
    interaction_id: str


@dataclass(frozen=True, slots=True)
class PublishMatchResultApplicationCommand:
    race_id: int
    revision_number: int
    guild_id: str
    actor_discord_user_id: str
    interaction_id: str
    authorize_delivery_unknown_retry: bool = False


@dataclass(frozen=True, slots=True)
class MatchResultQuery:
    race_id: int
    revision_number: int | None = None


@dataclass(frozen=True, slots=True)
class RecordMatchPublicationDeliveryCommand:
    race_id: int
    publication_id: int
    delivery_status: str
    discord_message_id: str | None
    error_code: str | None
    actor_discord_user_id: str
    interaction_id: str
    delivery_attempt_count: int


def execute_match_settlement_confirmation(
    command: ConfirmMatchSettlementApplicationCommand,
) -> MatchSettlementOperationDTO:
    """Confirm immutable odds and settle one Race in the same transaction."""

    def operation(session) -> MatchSettlementOperationDTO:
        confirm_match_odds_snapshot(
            session,
            command=ConfirmMatchOddsSnapshotCommand(
                race_id=command.race_id,
                odds=command.odds,
                actor_discord_user_id=command.actor_discord_user_id,
                idempotency_key=f"room-settlement-odds:{command.interaction_id}",
                reason=command.reason,
            ),
        )
        return confirm_match_settlement(
            session,
            command=ConfirmMatchSettlementCommand(
                race_id=command.race_id,
                actor_discord_user_id=command.actor_discord_user_id,
                idempotency_key=f"room-settlement:{command.interaction_id}",
                reason=command.reason,
            ),
        )

    return run_application_command(operation)


def execute_match_settlement_rollback(
    command: RollbackMatchSettlementApplicationCommand,
) -> MatchSettlementRollbackDTO:
    """Terminally roll back one settled Race through one command root."""

    return run_application_command(
        lambda session: rollback_confirmed_match_settlement(
            session,
            command=RollbackMatchSettlementCommand(
                race_id=command.race_id,
                actor_discord_user_id=command.actor_discord_user_id,
                idempotency_key=f"room-settlement-rollback:{command.interaction_id}",
                reason=command.reason,
            ),
        )
    )


def execute_match_result_publication(
    command: PublishMatchResultApplicationCommand,
) -> MatchResultNotificationDTO:
    """Commit a publication intent and its durable notifications without sending Discord I/O."""

    def operation(session) -> MatchResultNotificationDTO:
        settings = get_guild_discord_settings(session, guild_id=command.guild_id)
        if not settings.room_match_announcements_enabled:
            raise MatchResultError("룸매치 결과 공개가 서버 설정에서 비활성화되어 있습니다.")
        if settings.room_match_announcement_channel_id is None:
            raise MatchResultError("룸매치 결과 공개 채널이 서버 설정에 필요합니다.")
        result = publish_match_result(
            session,
            command=PublishMatchResultCommand(
                race_id=command.race_id,
                revision_number=command.revision_number,
                target_channel_id=settings.room_match_announcement_channel_id,
                actor_discord_user_id=command.actor_discord_user_id,
                idempotency_key=f"room-result-publish:{command.interaction_id}",
                authorize_delivery_unknown_retry=command.authorize_delivery_unknown_retry,
            ),
        )
        return enqueue_match_result_notifications(
            session,
            operation=result,
            guild_id=command.guild_id,
            actor_discord_user_id=command.actor_discord_user_id,
            correlation_id=command.interaction_id,
        )

    return run_application_command(operation)


def query_match_result(command: MatchResultQuery) -> MatchResultOperationDTO:
    """Return a bounded result/publication snapshot through the read-only runner."""

    return run_application_query(
        lambda session: get_room_match_result(
            session,
            race_id=command.race_id,
            revision_number=command.revision_number,
        )
    )


def record_match_publication_delivery(
    command: RecordMatchPublicationDeliveryCommand,
) -> MatchResultOperationDTO | None:
    """Record the legacy Room Match publication outcome after Discord delivery."""

    base_key = (
        f"room-result-publish-outcome:{command.publication_id}:"
        f"{command.interaction_id}:{command.delivery_attempt_count}"
    )

    if command.delivery_status == "sent":
        if command.discord_message_id is None:
            raise MatchResultError("성공한 결과 공개에 Discord 메시지 ID가 없습니다.")
        outcome = MarkMatchResultPublicationSentCommand(
            race_id=command.race_id,
            publication_id=command.publication_id,
            discord_message_id=command.discord_message_id,
            actor_discord_user_id=command.actor_discord_user_id,
            idempotency_key=f"{base_key}:sent",
        )
        return run_application_command(lambda session: mark_match_result_publication_sent(session, command=outcome))

    if command.delivery_status == "delivery_unknown":
        outcome = MarkMatchResultPublicationUnknownCommand(
            race_id=command.race_id,
            publication_id=command.publication_id,
            error_code=command.error_code or "SEND_OUTCOME_UNKNOWN",
            actor_discord_user_id=command.actor_discord_user_id,
            idempotency_key=f"{base_key}:unknown",
        )
        return run_application_command(lambda session: mark_match_result_publication_unknown(session, command=outcome))

    if command.delivery_status in {"failed", "suppressed"}:
        outcome = MarkMatchResultPublicationFailedCommand(
            race_id=command.race_id,
            publication_id=command.publication_id,
            error_code=command.error_code or "PUBLICATION_NOT_DELIVERED",
            actor_discord_user_id=command.actor_discord_user_id,
            idempotency_key=f"{base_key}:failed",
        )
        return run_application_command(lambda session: mark_match_result_publication_failed(session, command=outcome))

    return None
