"""Create-only Discord guild provisioning and manifest boundary tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from uma_st2 import guild_provisioning_entrypoint
from uma_st2.adapters.cli import (
    DISCORD_GUILD_PROVISIONING_MANIFEST_SCHEMA,
    DiscordGuildProvisioningCliAdapter,
    DiscordGuildProvisioningCliError,
    DiscordGuildProvisioningCliRequest,
    load_discord_guild_provisioning_manifest,
    parse_discord_guild_provisioning_cli_request,
)
from uma_st2.application.discord import (
    DISCORD_GUILD_PROVISIONING_AUDIT_SCHEMA_VERSION,
    DISCORD_GUILD_PROVISIONING_OPERATION_TYPE,
    DiscordGuildProvisioningAuditError,
    DiscordGuildProvisioningCommands,
    DiscordGuildProvisioningConflictError,
    DiscordGuildProvisioningError,
    DiscordGuildProvisioningValues,
    DiscordGuildSettingsSnapshot,
    ProvisionDiscordGuildSettings,
    StoredDiscordGuildProvisioningOperation,
)
from uma_st2.application.execution import CommandRunner

NOW = datetime(2026, 8, 29, 8, 30, tzinfo=UTC)


def _values(**changes: object) -> DiscordGuildProvisioningValues:
    values: dict[str, object] = {
        "guild_id": "100000000000000001",
        "win5_announcement_channel_id": "200000000000000001",
        "match_announcement_channel_id": "200000000000000002",
        "log_channel_id": None,
        "operator_role_id": "300000000000000001",
        "bot_manager_role_id": None,
        "default_timezone": "Asia/Seoul",
        "win5_announcements_enabled": True,
        "match_announcements_enabled": True,
    }
    values.update(changes)
    return DiscordGuildProvisioningValues(**values)  # type: ignore[arg-type]


def _command(**value_changes: object) -> ProvisionDiscordGuildSettings:
    return ProvisionDiscordGuildSettings(
        values=_values(**value_changes),
        actor_discord_user_id="400000000000000001",
        reason="reviewed initial V2 runtime provisioning",
    )


class FakeDiscordGuildProvisioningRepository:
    def __init__(self) -> None:
        self.current: DiscordGuildSettingsSnapshot | None = None
        self.operation: StoredDiscordGuildProvisioningOperation | None = None
        self.create_calls = 0
        self.audit_calls = 0

    def find_operation(self, *, idempotency_key: str) -> StoredDiscordGuildProvisioningOperation | None:
        if self.operation is None:
            return None
        return self.operation if idempotency_key == self._idempotency_key else None

    def lock_settings(self, *, guild_id: str) -> DiscordGuildSettingsSnapshot | None:
        if self.current is None or self.current.values.guild_id != guild_id:
            return None
        return self.current

    def create_settings(
        self,
        *,
        values: DiscordGuildProvisioningValues,
        created_at: datetime,
    ) -> DiscordGuildSettingsSnapshot:
        self.create_calls += 1
        self.current = DiscordGuildSettingsSnapshot(values=values, created_at=created_at, updated_at=created_at)
        return self.current

    def add_audit(
        self,
        *,
        command: ProvisionDiscordGuildSettings,
        snapshot: DiscordGuildSettingsSnapshot,
        created_at: datetime,
    ) -> int:
        assert created_at == snapshot.created_at
        self.audit_calls += 1
        self._idempotency_key = command.idempotency_key
        self.operation = StoredDiscordGuildProvisioningOperation(
            operation_id=71,
            request_fingerprint=command.request_fingerprint,
            type=DISCORD_GUILD_PROVISIONING_OPERATION_TYPE,
            after_data={
                "schema_version": DISCORD_GUILD_PROVISIONING_AUDIT_SCHEMA_VERSION,
                "settings": snapshot.to_payload(),
            },
        )
        return 71


class FakeDiscordGuildProvisioningUnitOfWork:
    def __init__(self, repository: FakeDiscordGuildProvisioningRepository) -> None:
        self.discord_guild_provisioning = repository
        self.commits = 0

    def __enter__(self) -> FakeDiscordGuildProvisioningUnitOfWork:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None


def _commands(repository: FakeDiscordGuildProvisioningRepository) -> DiscordGuildProvisioningCommands:
    return DiscordGuildProvisioningCommands(
        CommandRunner(lambda: FakeDiscordGuildProvisioningUnitOfWork(repository)),
        clock=lambda: NOW,
    )


def test_provisioning_creates_one_settings_row_and_exact_retry_uses_audit() -> None:
    repository = FakeDiscordGuildProvisioningRepository()
    commands = _commands(repository)
    command = _command()

    created = commands.provision(command)
    retried = commands.provision(command)

    assert created.created is True
    assert retried.created is False
    assert created.operation_id == retried.operation_id == 71
    assert retried.snapshot == created.snapshot
    assert repository.create_calls == 1
    assert repository.audit_calls == 1
    assert len(command.idempotency_key) <= 128


def test_existing_settings_cannot_be_overwritten_by_another_manifest() -> None:
    repository = FakeDiscordGuildProvisioningRepository()
    commands = _commands(repository)
    commands.provision(_command())

    with pytest.raises(DiscordGuildProvisioningConflictError, match="cannot overwrite"):
        commands.provision(_command(match_announcements_enabled=False))

    assert repository.create_calls == 1
    assert repository.audit_calls == 1


def test_provisioning_allows_publication_destinations_to_be_unset() -> None:
    values = _values(
        win5_announcement_channel_id=None,
        match_announcement_channel_id=None,
        log_channel_id=None,
    )

    assert values.win5_announcement_channel_id is None
    assert values.match_announcement_channel_id is None
    assert values.log_channel_id is None
    assert DiscordGuildProvisioningValues.from_payload(values.to_payload()) == values


def test_malformed_exact_retry_audit_fails_closed() -> None:
    repository = FakeDiscordGuildProvisioningRepository()
    commands = _commands(repository)
    command = _command()
    commands.provision(command)
    assert repository.operation is not None
    repository.operation = StoredDiscordGuildProvisioningOperation(
        operation_id=71,
        request_fingerprint=command.request_fingerprint,
        type=DISCORD_GUILD_PROVISIONING_OPERATION_TYPE,
        after_data={"schema_version": 999, "settings": repository.current.to_payload()},  # type: ignore[union-attr]
    )

    with pytest.raises(DiscordGuildProvisioningAuditError, match="unsupported"):
        commands.provision(command)


@pytest.mark.parametrize(
    "changes",
    [
        {"win5_announcement_channel_id": "0"},
        {"operator_role_id": None, "bot_manager_role_id": None},
        {"operator_role_id": "100000000000000001"},
        {"default_timezone": "Not/A-Timezone"},
    ],
)
def test_provisioning_values_fail_closed_before_opening_a_uow(changes: dict[str, object]) -> None:
    with pytest.raises(DiscordGuildProvisioningError):
        _values(**changes)


def _manifest(**changes: object) -> dict[str, object]:
    payload = {
        "schema": DISCORD_GUILD_PROVISIONING_MANIFEST_SCHEMA,
        **_values().to_payload(),
        "actor_discord_user_id": "400000000000000001",
        "reason": "reviewed initial V2 runtime provisioning",
    }
    payload.update(changes)
    return payload


def _write_manifest(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_manifest_parser_is_strict_and_builds_deterministic_command(tmp_path: Path) -> None:
    path = tmp_path / "guild-settings.json"
    _write_manifest(path, _manifest())

    first = load_discord_guild_provisioning_manifest(path)
    second = load_discord_guild_provisioning_manifest(path)

    assert first == second
    assert first.values == _values()
    assert parse_discord_guild_provisioning_cli_request(["--manifest", str(path)]) == (
        DiscordGuildProvisioningCliRequest(manifest_path=path)
    )


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {**_manifest(), "unexpected": True},
        {**_manifest(), "schema": "discord-guild-provision/v2"},
        {**_manifest(), "win5_announcements_enabled": "true"},
    ],
)
def test_manifest_rejects_wrong_shape_schema_and_types(tmp_path: Path, payload: object) -> None:
    path = tmp_path / "guild-settings.json"
    _write_manifest(path, payload)

    with pytest.raises(DiscordGuildProvisioningCliError):
        load_discord_guild_provisioning_manifest(path)


def test_manifest_rejects_duplicate_keys_and_oversized_input(tmp_path: Path) -> None:
    duplicate_path = tmp_path / "duplicate.json"
    serialized = json.dumps(_manifest())
    duplicate_path.write_text(
        '{"schema":"discord-guild-provision/v1",' + serialized[1:],
        encoding="utf-8",
    )

    with pytest.raises(DiscordGuildProvisioningCliError, match="duplicate JSON keys"):
        load_discord_guild_provisioning_manifest(duplicate_path)

    oversized_path = tmp_path / "oversized.json"
    oversized_path.write_bytes(b" " * 65_537)

    with pytest.raises(DiscordGuildProvisioningCliError, match="size is invalid"):
        load_discord_guild_provisioning_manifest(oversized_path)


class FakeProvisioningCommands:
    def __init__(self, receipt: object) -> None:
        self.receipt = receipt
        self.calls: list[ProvisionDiscordGuildSettings] = []

    def provision(self, command: ProvisionDiscordGuildSettings) -> object:
        self.calls.append(command)
        return self.receipt


class FakeSettings:
    database_url_value = "mysql+pymysql://user:password@db/test?charset=utf8mb4"


class FakeDatabaseRuntime:
    def __init__(self) -> None:
        self.dispose_calls = 0

    def dispose(self) -> None:
        self.dispose_calls += 1


@pytest.mark.parametrize("failure", [None, RuntimeError("provision failed")])
def test_entrypoint_always_disposes_database_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: Exception | None,
) -> None:
    path = tmp_path / "guild-settings.json"
    _write_manifest(path, _manifest())
    repository = FakeDiscordGuildProvisioningRepository()
    real_commands = _commands(repository)
    database_runtime = FakeDatabaseRuntime()

    class Commands:
        def provision(self, command: ProvisionDiscordGuildSettings) -> object:
            if failure is not None:
                raise failure
            return real_commands.provision(command)

    monkeypatch.setattr(
        guild_provisioning_entrypoint.DatabaseRuntime,
        "from_url",
        classmethod(lambda cls, *_args, **_kwargs: database_runtime),
    )
    monkeypatch.setattr(
        guild_provisioning_entrypoint,
        "compose_discord_guild_provisioning",
        lambda _runtime: Commands(),
    )
    request = DiscordGuildProvisioningCliRequest(manifest_path=path)

    if failure is None:
        receipt = guild_provisioning_entrypoint.run_discord_guild_provisioning(
            FakeSettings(),  # type: ignore[arg-type]
            request,
        )
        assert receipt.created is True
    else:
        with pytest.raises(RuntimeError, match="provision failed"):
            guild_provisioning_entrypoint.run_discord_guild_provisioning(
                FakeSettings(),  # type: ignore[arg-type]
                request,
            )

    assert database_runtime.dispose_calls == 1


def test_cli_adapter_loads_manifest_before_calling_application(tmp_path: Path) -> None:
    path = tmp_path / "guild-settings.json"
    _write_manifest(path, _manifest())
    commands = FakeProvisioningCommands(receipt="receipt")
    adapter = DiscordGuildProvisioningCliAdapter(commands=commands)  # type: ignore[arg-type]

    assert adapter.execute(DiscordGuildProvisioningCliRequest(path)) == "receipt"
    assert commands.calls == [_command()]
