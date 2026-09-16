"""Application-owned command/query Unit-of-Work semantics."""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner, QueryRunner


@dataclass
class RecordingUnitOfWork:
    entered: bool = False
    exited: bool = False
    commit_count: int = 0
    rollback_count: int = 0

    def __enter__(self) -> RecordingUnitOfWork:
        self.entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if self.commit_count == 0 and self.rollback_count == 0:
            self.rollback_count += 1
        self.exited = True
        return False

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


class RecordingFactory:
    def __init__(self) -> None:
        self.created: list[RecordingUnitOfWork] = []

    def __call__(self) -> RecordingUnitOfWork:
        unit_of_work = RecordingUnitOfWork()
        self.created.append(unit_of_work)
        return unit_of_work


def test_command_runner_commits_once_and_returns_result() -> None:
    factory = RecordingFactory()
    runner = CommandRunner(factory)

    result = runner.run(lambda unit_of_work: "done" if unit_of_work.entered else "not-entered")

    assert result == "done"
    assert factory.created == [RecordingUnitOfWork(entered=True, exited=True, commit_count=1, rollback_count=0)]


def test_command_runner_rolls_back_and_propagates_operation_failure() -> None:
    factory = RecordingFactory()
    runner = CommandRunner(factory)

    def fail(_unit_of_work: RecordingUnitOfWork) -> None:
        raise ValueError("operation failed")

    with pytest.raises(ValueError, match="operation failed"):
        runner.run(fail)

    assert factory.created == [RecordingUnitOfWork(entered=True, exited=True, commit_count=0, rollback_count=1)]


def test_query_runner_explicitly_rolls_back_instead_of_committing() -> None:
    factory = RecordingFactory()
    runner = QueryRunner(factory)

    assert runner.run(lambda _unit_of_work: 42) == 42
    assert factory.created == [RecordingUnitOfWork(entered=True, exited=True, commit_count=0, rollback_count=1)]


def test_each_runner_call_uses_a_fresh_unit_of_work() -> None:
    factory = RecordingFactory()
    runner = QueryRunner(factory)

    runner.run(lambda _unit_of_work: None)
    runner.run(lambda _unit_of_work: None)

    assert len(factory.created) == 2
    assert factory.created[0] is not factory.created[1]
