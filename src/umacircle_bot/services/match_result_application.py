from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from umacircle_bot.domain.match_results import MatchResultInput
from umacircle_bot.services.application import run_application_command, run_application_query
from umacircle_bot.services.match_result_notifications import (
    MatchResultNotificationDTO,
    enqueue_match_result_notifications,
)
from umacircle_bot.services.match_results import (
    ConfirmMatchResultCommand,
    CorrectMatchResultCommand,
    MatchResultOperationDTO,
    RejectMatchResultCommand,
    ReviewMatchResultCommand,
    SubmitMatchResultCommand,
    confirm_match_result,
    correct_match_result,
    get_room_match_result,
    reject_match_result,
    review_match_result,
    submit_match_result,
)

MatchResultMutation = Callable[[Session], MatchResultOperationDTO]


def execute_match_result_submit(
    *,
    race_id: int,
    match_type: str,
    results: tuple[MatchResultInput, ...],
    reason: str | None,
    guild_id: str,
    actor_discord_user_id: str,
    interaction_id: str,
) -> MatchResultNotificationDTO:
    command = SubmitMatchResultCommand(
        race_id=race_id,
        match_type=match_type,
        results=results,
        actor_discord_user_id=actor_discord_user_id,
        idempotency_key=f"room-result-submit:{interaction_id}",
        reason=reason,
    )
    return _execute_notified_mutation(
        mutation=lambda session: submit_match_result(session, command=command),
        guild_id=guild_id,
        actor_discord_user_id=actor_discord_user_id,
        correlation_id=interaction_id,
    )


def execute_match_result_review(
    *,
    race_id: int,
    revision_number: int,
    reason: str | None,
    guild_id: str,
    actor_discord_user_id: str,
    interaction_id: str,
) -> MatchResultNotificationDTO:
    command = ReviewMatchResultCommand(
        race_id=race_id,
        revision_number=revision_number,
        actor_discord_user_id=actor_discord_user_id,
        idempotency_key=f"room-result-review:{interaction_id}",
        reason=reason,
    )
    return _execute_notified_mutation(
        mutation=lambda session: review_match_result(session, command=command),
        guild_id=guild_id,
        actor_discord_user_id=actor_discord_user_id,
        correlation_id=interaction_id,
    )


def execute_match_result_correct(
    *,
    race_id: int,
    revision_number: int,
    match_type: str,
    results: tuple[MatchResultInput, ...],
    reason: str | None,
    guild_id: str,
    actor_discord_user_id: str,
    interaction_id: str,
) -> MatchResultNotificationDTO:
    command = CorrectMatchResultCommand(
        race_id=race_id,
        revision_number=revision_number,
        match_type=match_type,
        results=results,
        actor_discord_user_id=actor_discord_user_id,
        idempotency_key=f"room-result-correct:{interaction_id}",
        reason=reason,
    )
    return _execute_notified_mutation(
        mutation=lambda session: correct_match_result(session, command=command),
        guild_id=guild_id,
        actor_discord_user_id=actor_discord_user_id,
        correlation_id=interaction_id,
    )


def execute_match_result_reject(
    *,
    race_id: int,
    revision_number: int,
    reason: str,
    guild_id: str,
    actor_discord_user_id: str,
    interaction_id: str,
) -> MatchResultNotificationDTO:
    command = RejectMatchResultCommand(
        race_id=race_id,
        revision_number=revision_number,
        actor_discord_user_id=actor_discord_user_id,
        idempotency_key=f"room-result-reject:{interaction_id}",
        reason=reason,
    )
    return _execute_notified_mutation(
        mutation=lambda session: reject_match_result(session, command=command),
        guild_id=guild_id,
        actor_discord_user_id=actor_discord_user_id,
        correlation_id=interaction_id,
    )


def execute_match_result_confirm(
    *,
    race_id: int,
    revision_number: int,
    reason: str | None,
    guild_id: str,
    actor_discord_user_id: str,
    interaction_id: str,
) -> MatchResultNotificationDTO:
    command = ConfirmMatchResultCommand(
        race_id=race_id,
        revision_number=revision_number,
        actor_discord_user_id=actor_discord_user_id,
        idempotency_key=f"room-result-confirm:{interaction_id}",
        reason=reason,
    )
    return _execute_notified_mutation(
        mutation=lambda session: confirm_match_result(session, command=command),
        guild_id=guild_id,
        actor_discord_user_id=actor_discord_user_id,
        correlation_id=interaction_id,
    )


def query_match_result_show(
    *,
    race_id: int,
    revision_number: int | None = None,
) -> MatchResultOperationDTO:
    return run_application_query(
        lambda session: get_room_match_result(
            session,
            race_id=race_id,
            revision_number=revision_number,
        )
    )


def _execute_notified_mutation(
    *,
    mutation: MatchResultMutation,
    guild_id: str,
    actor_discord_user_id: str,
    correlation_id: str,
) -> MatchResultNotificationDTO:
    def operation(session: Session) -> MatchResultNotificationDTO:
        result = mutation(session)
        return enqueue_match_result_notifications(
            session,
            operation=result,
            guild_id=guild_id,
            actor_discord_user_id=actor_discord_user_id,
            correlation_id=correlation_id,
        )

    return run_application_command(operation)
