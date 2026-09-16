"""Bounded operational CLI for shared publication reconciliation."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass

from uma_st2.application.publication import (
    PUBLICATION_AWAITING_PROMOTION_MAX_COUNT,
    PUBLICATION_UNKNOWN_LIST_MAX_COUNT,
    SUPPORTED_PUBLICATION_DESTINATION_KINDS,
    PromotedAwaitingPublications,
    PublicationDeliveryCommands,
    PublicationDeliveryQueries,
    PublicationUnknownResolution,
    ReconciledUnknownPublication,
    ReconcileUnknownPublication,
    UnknownPublicationDelivery,
)

PUBLICATION_AWAITING_PROMOTION_DEFAULT_COUNT = 100
PUBLICATION_UNKNOWN_LIST_DEFAULT_COUNT = 100


class PublicationReconciliationCliError(ValueError):
    """The local publication reconciliation request is invalid."""


def _positive_snowflake(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise PublicationReconciliationCliError(f"{field_name} must be a positive decimal Discord snowflake.")
    return value


def _batch_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be an integer.") from exc
    if not 1 <= parsed <= PUBLICATION_AWAITING_PROMOTION_MAX_COUNT:
        raise argparse.ArgumentTypeError(f"limit must be between 1 and {PUBLICATION_AWAITING_PROMOTION_MAX_COUNT}.")
    return parsed


def _positive_int(value: str, *, field_name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{field_name} must be an integer.") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"{field_name} must be a positive integer.")
    return parsed


@dataclass(frozen=True, slots=True)
class PublicationAwaitingPromotionCliRequest:
    guild_id: str
    destination_kind: str
    limit: int = PUBLICATION_AWAITING_PROMOTION_DEFAULT_COUNT

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "guild_id",
            _positive_snowflake(self.guild_id, field_name="guild_id"),
        )
        if self.destination_kind not in SUPPORTED_PUBLICATION_DESTINATION_KINDS:
            raise PublicationReconciliationCliError("destination_kind must be a supported publication destination.")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= PUBLICATION_AWAITING_PROMOTION_MAX_COUNT
        ):
            raise PublicationReconciliationCliError(
                f"limit must be between 1 and {PUBLICATION_AWAITING_PROMOTION_MAX_COUNT}."
            )


@dataclass(frozen=True, slots=True)
class PublicationUnknownListCliRequest:
    guild_id: str
    destination_kind: str
    limit: int = PUBLICATION_UNKNOWN_LIST_DEFAULT_COUNT

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "guild_id",
            _positive_snowflake(self.guild_id, field_name="guild_id"),
        )
        if self.destination_kind not in SUPPORTED_PUBLICATION_DESTINATION_KINDS:
            raise PublicationReconciliationCliError("destination_kind must be a supported publication destination.")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= PUBLICATION_UNKNOWN_LIST_MAX_COUNT
        ):
            raise PublicationReconciliationCliError(
                f"limit must be between 1 and {PUBLICATION_UNKNOWN_LIST_MAX_COUNT}."
            )


@dataclass(frozen=True, slots=True)
class PublicationUnknownReconciliationCliRequest:
    guild_id: str
    destination_kind: str
    publication_id: int
    attempt_count: int
    resolution: PublicationUnknownResolution
    discord_message_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "guild_id",
            _positive_snowflake(self.guild_id, field_name="guild_id"),
        )
        if self.destination_kind not in SUPPORTED_PUBLICATION_DESTINATION_KINDS:
            raise PublicationReconciliationCliError("destination_kind must be a supported publication destination.")
        for field_name in ("publication_id", "attempt_count"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise PublicationReconciliationCliError(f"{field_name} must be a positive integer.")
        try:
            resolution = PublicationUnknownResolution(self.resolution)
        except ValueError as exc:
            raise PublicationReconciliationCliError("resolution is unsupported.") from exc
        object.__setattr__(self, "resolution", resolution)
        if self.discord_message_id is not None:
            object.__setattr__(
                self,
                "discord_message_id",
                _positive_snowflake(
                    self.discord_message_id,
                    field_name="discord_message_id",
                ),
            )
        if resolution == PublicationUnknownResolution.CONFIRMED_SENT:
            if self.discord_message_id is None:
                raise PublicationReconciliationCliError("discord_message_id is required for confirmed sent.")
        elif self.discord_message_id is not None:
            raise PublicationReconciliationCliError("discord_message_id is not allowed for zero-send retry.")


PublicationReconciliationCliRequest = (
    PublicationAwaitingPromotionCliRequest
    | PublicationUnknownListCliRequest
    | PublicationUnknownReconciliationCliRequest
)
PublicationReconciliationCliResult = (
    PromotedAwaitingPublications | tuple[UnknownPublicationDelivery, ...] | ReconciledUnknownPublication
)


def parse_publication_reconciliation_cli_request(
    argv: Sequence[str] | None = None,
) -> PublicationReconciliationCliRequest:
    parser = argparse.ArgumentParser(
        prog="uma-st-2-reconcile-publications",
        description="Run one bounded operational publication reconciliation action.",
    )
    actions = parser.add_subparsers(dest="action", required=True)
    promote = actions.add_parser(
        "promote-awaiting",
        help="Promote awaiting-channel rows using the current canonical guild setting.",
    )
    promote.add_argument("--guild-id", required=True)
    promote.add_argument(
        "--destination-kind",
        required=True,
        choices=sorted(SUPPORTED_PUBLICATION_DESTINATION_KINDS),
    )
    promote.add_argument(
        "--limit",
        type=_batch_limit,
        default=PUBLICATION_AWAITING_PROMOTION_DEFAULT_COUNT,
    )
    list_unknown = actions.add_parser(
        "list-unknown",
        help="List bounded unknown-delivery evidence for operator inspection.",
    )
    list_unknown.add_argument("--guild-id", required=True)
    list_unknown.add_argument(
        "--destination-kind",
        required=True,
        choices=sorted(SUPPORTED_PUBLICATION_DESTINATION_KINDS),
    )
    list_unknown.add_argument(
        "--limit",
        type=_batch_limit,
        default=PUBLICATION_UNKNOWN_LIST_DEFAULT_COUNT,
    )
    mark_sent = actions.add_parser(
        "mark-unknown-sent",
        help="Confirm that every page of one unknown logical publication was delivered.",
    )
    retry_zero_send = actions.add_parser(
        "retry-unknown-zero-send",
        help="Requeue one anchor-free unknown after confirming that no page was delivered.",
    )
    for action in (mark_sent, retry_zero_send):
        action.add_argument("--guild-id", required=True)
        action.add_argument(
            "--destination-kind",
            required=True,
            choices=sorted(SUPPORTED_PUBLICATION_DESTINATION_KINDS),
        )
        action.add_argument(
            "--publication-id",
            required=True,
            type=lambda value: _positive_int(value, field_name="publication_id"),
        )
        action.add_argument(
            "--attempt-count",
            required=True,
            type=lambda value: _positive_int(value, field_name="attempt_count"),
        )
    mark_sent.add_argument("--discord-message-id", required=True)
    namespace = parser.parse_args(argv)
    if namespace.action == "promote-awaiting":
        return PublicationAwaitingPromotionCliRequest(
            guild_id=namespace.guild_id,
            destination_kind=namespace.destination_kind,
            limit=namespace.limit,
        )
    if namespace.action == "list-unknown":
        return PublicationUnknownListCliRequest(
            guild_id=namespace.guild_id,
            destination_kind=namespace.destination_kind,
            limit=namespace.limit,
        )
    if namespace.action == "mark-unknown-sent":
        return PublicationUnknownReconciliationCliRequest(
            guild_id=namespace.guild_id,
            destination_kind=namespace.destination_kind,
            publication_id=namespace.publication_id,
            attempt_count=namespace.attempt_count,
            resolution=PublicationUnknownResolution.CONFIRMED_SENT,
            discord_message_id=namespace.discord_message_id,
        )
    if namespace.action == "retry-unknown-zero-send":
        return PublicationUnknownReconciliationCliRequest(
            guild_id=namespace.guild_id,
            destination_kind=namespace.destination_kind,
            publication_id=namespace.publication_id,
            attempt_count=namespace.attempt_count,
            resolution=PublicationUnknownResolution.RETRY_ZERO_SEND,
        )
    raise PublicationReconciliationCliError("Publication reconciliation action is unsupported.")


@dataclass(frozen=True, slots=True)
class PublicationReconciliationCliAdapter:
    commands: PublicationDeliveryCommands
    queries: PublicationDeliveryQueries

    def execute(
        self,
        request: PublicationReconciliationCliRequest,
    ) -> PublicationReconciliationCliResult:
        if isinstance(request, PublicationAwaitingPromotionCliRequest):
            return self.commands.promote_awaiting(
                guild_id=request.guild_id,
                destination_kind=request.destination_kind,
                max_count=request.limit,
            )
        if isinstance(request, PublicationUnknownListCliRequest):
            return self.queries.list_unknown(
                guild_id=request.guild_id,
                destination_kind=request.destination_kind,
                max_count=request.limit,
            )
        if isinstance(request, PublicationUnknownReconciliationCliRequest):
            return self.commands.reconcile_unknown(
                ReconcileUnknownPublication(
                    publication_id=request.publication_id,
                    guild_id=request.guild_id,
                    destination_kind=request.destination_kind,
                    expected_attempt_count=request.attempt_count,
                    resolution=request.resolution,
                    discord_message_id=request.discord_message_id,
                )
            )
        raise PublicationReconciliationCliError("request has an unsupported publication reconciliation type.")
