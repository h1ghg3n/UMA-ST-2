"""Reviewed manifest adapter for one-shot Discord guild provisioning."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from uma_st2.application.discord import (
    DiscordGuildProvisioningCommands,
    DiscordGuildProvisioningValues,
    ProvisionDiscordGuildSettings,
    ProvisionedDiscordGuildSettings,
)

DISCORD_GUILD_PROVISIONING_MANIFEST_SCHEMA = "discord-guild-provision/v1"
_MAX_MANIFEST_BYTES = 65_536


class DiscordGuildProvisioningCliError(ValueError):
    """The local provisioning request or manifest is invalid."""


def _reject_duplicate_object_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise DiscordGuildProvisioningCliError(
                "The Discord guild provisioning manifest contains duplicate JSON keys."
            )
        payload[key] = value
    return payload


@dataclass(frozen=True, slots=True)
class DiscordGuildProvisioningCliRequest:
    manifest_path: Path


def parse_discord_guild_provisioning_cli_request(
    argv: Sequence[str] | None = None,
) -> DiscordGuildProvisioningCliRequest:
    parser = argparse.ArgumentParser(
        prog="uma-st-2-provision-discord",
        description="Provision one reviewed Discord guild settings manifest into a fresh V2 database.",
    )
    parser.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="UTF-8 JSON manifest using discord-guild-provision/v1",
    )
    namespace = parser.parse_args(argv)
    return DiscordGuildProvisioningCliRequest(manifest_path=namespace.manifest)


def load_discord_guild_provisioning_manifest(path: Path) -> ProvisionDiscordGuildSettings:
    """Read one bounded strict JSON manifest into the Application command."""

    try:
        with path.open("rb") as manifest:
            content = manifest.read(_MAX_MANIFEST_BYTES + 1)
    except OSError as exc:
        raise DiscordGuildProvisioningCliError("Could not read the Discord guild provisioning manifest.") from exc
    if not content or len(content) > _MAX_MANIFEST_BYTES:
        raise DiscordGuildProvisioningCliError("The Discord guild provisioning manifest size is invalid.")
    try:
        payload = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object_keys,
        )
    except DiscordGuildProvisioningCliError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiscordGuildProvisioningCliError(
            "The Discord guild provisioning manifest is not valid UTF-8 JSON."
        ) from exc
    if not isinstance(payload, Mapping):
        raise DiscordGuildProvisioningCliError("The Discord guild provisioning manifest must be a JSON object.")
    expected = {
        "schema",
        "guild_id",
        "win5_announcement_channel_id",
        "match_announcement_channel_id",
        "log_channel_id",
        "operator_role_id",
        "bot_manager_role_id",
        "default_timezone",
        "win5_announcements_enabled",
        "match_announcements_enabled",
        "actor_discord_user_id",
        "reason",
    }
    if set(payload) != expected or payload["schema"] != DISCORD_GUILD_PROVISIONING_MANIFEST_SCHEMA:
        raise DiscordGuildProvisioningCliError("The Discord guild provisioning manifest schema or keys are invalid.")
    try:
        return ProvisionDiscordGuildSettings(
            values=DiscordGuildProvisioningValues(
                guild_id=payload["guild_id"],  # type: ignore[arg-type]
                win5_announcement_channel_id=payload["win5_announcement_channel_id"],  # type: ignore[arg-type]
                match_announcement_channel_id=payload["match_announcement_channel_id"],  # type: ignore[arg-type]
                log_channel_id=payload["log_channel_id"],  # type: ignore[arg-type]
                operator_role_id=payload["operator_role_id"],  # type: ignore[arg-type]
                bot_manager_role_id=payload["bot_manager_role_id"],  # type: ignore[arg-type]
                default_timezone=payload["default_timezone"],  # type: ignore[arg-type]
                win5_announcements_enabled=payload["win5_announcements_enabled"],  # type: ignore[arg-type]
                match_announcements_enabled=payload["match_announcements_enabled"],  # type: ignore[arg-type]
            ),
            actor_discord_user_id=payload["actor_discord_user_id"],  # type: ignore[arg-type]
            reason=payload["reason"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise DiscordGuildProvisioningCliError("The Discord guild provisioning manifest values are invalid.") from exc


@dataclass(frozen=True, slots=True)
class DiscordGuildProvisioningCliAdapter:
    commands: DiscordGuildProvisioningCommands

    def execute(self, request: DiscordGuildProvisioningCliRequest) -> ProvisionedDiscordGuildSettings:
        command = load_discord_guild_provisioning_manifest(request.manifest_path)
        return self.commands.provision(command)
