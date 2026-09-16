"""Executable boundary for bounded publication reconciliation."""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence

from uma_st2.adapters.cli import (
    PublicationReconciliationCliAdapter,
    PublicationReconciliationCliError,
    PublicationReconciliationCliRequest,
    PublicationReconciliationCliResult,
    parse_publication_reconciliation_cli_request,
)
from uma_st2.application.publication import (
    PromotedAwaitingPublications,
    PublicationDeliveryError,
    ReconciledUnknownPublication,
    UnknownPublicationDelivery,
)
from uma_st2.compose import (
    compose_publication_delivery,
    compose_publication_delivery_queries,
)
from uma_st2.config import DatabaseSettings
from uma_st2.infrastructure.database import DatabaseRuntime

logger = logging.getLogger(__name__)


def run_publication_reconciliation(
    settings: DatabaseSettings,
    request: PublicationReconciliationCliRequest,
) -> PublicationReconciliationCliResult:
    """Compose one Engine, execute one bounded repair, and always dispose resources."""

    database_runtime = DatabaseRuntime.from_url(
        settings.database_url_value,
        pool_pre_ping=True,
    )
    try:
        adapter = PublicationReconciliationCliAdapter(
            commands=compose_publication_delivery(database_runtime),
            queries=compose_publication_delivery_queries(database_runtime),
        )
        return adapter.execute(request)
    finally:
        database_runtime.dispose()


def _print_unknown(row: UnknownPublicationDelivery) -> None:
    anchor = row.discord_message_id or "-"
    print(
        " ".join(
            (
                f"publication_id={row.publication_id}",
                f"event_type={row.event_type}",
                f"event_key={row.event_key}",
                f"source={row.source_kind}:{row.source_id}",
                f"channel={row.target_channel_id}",
                f"attempt_count={row.attempt_count}",
                f"anchor={anchor}",
                f"error={row.error_code}",
                f"stage={row.failure_stage.value}",
                f"fingerprint={row.payload_fingerprint}",
                f"updated_at={row.updated_at.isoformat()}",
            )
        )
    )


def _print_success(result: PublicationReconciliationCliResult) -> None:
    if isinstance(result, PromotedAwaitingPublications):
        print(f"Promoted awaiting publications: {result.promoted_count}")
        print(f"Guild: {result.guild_id}")
        print(f"Destination: {result.destination_kind}")
        return
    if isinstance(result, ReconciledUnknownPublication):
        print(f"Reconciled unknown publication: {result.publication_id}")
        print(f"Resolution: {result.resolution.value}")
        print(f"Status: {result.status.value}")
        print(f"Attempt count: {result.attempt_count}")
        if result.discord_message_id is not None:
            print(f"Discord message anchor: {result.discord_message_id}")
        return
    print(f"Unknown publications: {len(result)}")
    for row in result:
        _print_unknown(row)


def main(argv: Sequence[str] | None = None) -> None:
    try:
        request = parse_publication_reconciliation_cli_request(argv)
        settings = DatabaseSettings()
        receipt = run_publication_reconciliation(settings, request)
    except PublicationReconciliationCliError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except PublicationDeliveryError:
        print(
            "Publication reconciliation was rejected: check current guild settings and stored delivery state.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except Exception as exc:
        logger.critical(
            "Publication reconciliation failed error_type=%s",
            type(exc).__name__,
        )
        print("Publication reconciliation failed due to an internal error.", file=sys.stderr)
        raise SystemExit(1) from None
    _print_success(receipt)


if __name__ == "__main__":
    main()
