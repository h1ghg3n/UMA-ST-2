"""Shared bridge from Discord coroutines to synchronous application ports."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol, TypeVar

ResultT = TypeVar("ResultT")


class BlockingApplicationRunner(Protocol):
    """Run a synchronous application call outside Discord's event loop."""

    async def __call__(self, operation: Callable[[], ResultT]) -> ResultT: ...


async def run_blocking_application[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    """Run one synchronous application operation in the shared worker bridge."""

    return await asyncio.to_thread(operation)
