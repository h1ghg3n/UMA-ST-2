"""Tests for the executable process ownership boundary."""

from __future__ import annotations

import pytest

from uma_st2 import entrypoint


class FakeSettings:
    def resolve_discord_token(self) -> str:
        return "token.value.signature"


class FakeRuntime:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.tokens: list[str] = []
        self.dispose_calls = 0

    def run(self, token: str) -> None:
        self.tokens.append(token)
        if self.failure is not None:
            raise self.failure

    def dispose(self) -> None:
        self.dispose_calls += 1


def test_entrypoint_disposes_runtime_after_normal_client_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FakeRuntime()
    monkeypatch.setattr(entrypoint, "compose_discord_runtime", lambda _settings: runtime)

    entrypoint.run_discord_runtime(FakeSettings())  # type: ignore[arg-type]

    assert runtime.tokens == ["token.value.signature"]
    assert runtime.dispose_calls == 1


def test_entrypoint_disposes_runtime_after_client_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FakeRuntime(failure=RuntimeError("client failed"))
    monkeypatch.setattr(entrypoint, "compose_discord_runtime", lambda _settings: runtime)

    with pytest.raises(RuntimeError, match="client failed"):
        entrypoint.run_discord_runtime(FakeSettings())  # type: ignore[arg-type]

    assert runtime.dispose_calls == 1
