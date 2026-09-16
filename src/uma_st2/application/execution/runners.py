"""Transaction runners for mutation and read-only application operations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from .uow import UnitOfWork, UnitOfWorkFactory

ResultT = TypeVar("ResultT")


@dataclass(frozen=True, slots=True)
class CommandRunner[UnitOfWorkT: UnitOfWork]:
    """Run one mutation in a fresh unit of work and commit it once."""

    unit_of_work_factory: UnitOfWorkFactory[UnitOfWorkT]

    def run(self, operation: Callable[[UnitOfWorkT], ResultT]) -> ResultT:
        with self.unit_of_work_factory() as unit_of_work:
            result = operation(unit_of_work)
            unit_of_work.commit()
            return result


@dataclass(frozen=True, slots=True)
class QueryRunner[UnitOfWorkT: UnitOfWork]:
    """Run one read-only operation without committing incidental changes."""

    unit_of_work_factory: UnitOfWorkFactory[UnitOfWorkT]

    def run(self, operation: Callable[[UnitOfWorkT], ResultT]) -> ResultT:
        with self.unit_of_work_factory() as unit_of_work:
            result = operation(unit_of_work)
            unit_of_work.rollback()
            return result
