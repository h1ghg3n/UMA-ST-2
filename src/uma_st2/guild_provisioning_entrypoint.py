"""Executable one-shot boundary for reviewed Discord guild provisioning."""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence

from uma_st2.adapters.cli import (
    DiscordGuildProvisioningCliAdapter,
    DiscordGuildProvisioningCliError,
    DiscordGuildProvisioningCliRequest,
    parse_discord_guild_provisioning_cli_request,
)
from uma_st2.application.discord import (
    DiscordGuildProvisioningError,
    ProvisionedDiscordGuildSettings,
)
from uma_st2.compose import compose_discord_guild_provisioning
from uma_st2.config import DatabaseSettings
from uma_st2.infrastructure.database import DatabaseRuntime

logger = logging.getLogger(__name__)


def run_discord_guild_provisioning(
    settings: DatabaseSettings,
    request: DiscordGuildProvisioningCliRequest,
) -> ProvisionedDiscordGuildSettings:
    """Compose one Engine, execute one manifest, and always dispose resources."""

    database_runtime = DatabaseRuntime.from_url(
        settings.database_url_value,
        pool_pre_ping=True,
    )
    try:
        adapter = DiscordGuildProvisioningCliAdapter(
            commands=compose_discord_guild_provisioning(database_runtime),
        )
        return adapter.execute(request)
    finally:
        database_runtime.dispose()


def _print_success(receipt: ProvisionedDiscordGuildSettings) -> None:
    status = "created" if receipt.created else "exact retry"
    print(f"Discord guild settings {status}: {receipt.snapshot.values.guild_id}")
    print(f"Operation ID: {receipt.operation_id}")


def main(argv: Sequence[str] | None = None) -> None:
    request = parse_discord_guild_provisioning_cli_request(argv)
    try:
        settings = DatabaseSettings()
        receipt = run_discord_guild_provisioning(settings, request)
    except DiscordGuildProvisioningCliError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except DiscordGuildProvisioningError:
        print(
            "Discord guild provisioning was rejected: check the reviewed manifest and existing settings.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except Exception as exc:
        logger.critical(
            "Discord guild provisioning failed error_type=%s",
            type(exc).__name__,
        )
        print("Discord guild provisioning failed due to an internal error.", file=sys.stderr)
        raise SystemExit(1) from None
    _print_success(receipt)


if __name__ == "__main__":
    main()
