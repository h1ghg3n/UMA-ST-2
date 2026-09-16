"""Application-owned Unit-of-Work ports."""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self, TypeVar


class UnitOfWork(Protocol):
    """Logical transaction boundary used by application operations.

    Concrete session and transaction mechanics belong to infrastructure. An
    uncommitted unit of work must roll back when its context exits.
    """

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


UnitOfWorkT_co = TypeVar("UnitOfWorkT_co", bound=UnitOfWork, covariant=True)


class UnitOfWorkFactory(Protocol[UnitOfWorkT_co]):
    """Create one fresh unit of work for one application operation."""

    def __call__(self) -> UnitOfWorkT_co: ...
